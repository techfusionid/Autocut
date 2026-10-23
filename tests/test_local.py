from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from autocut_api.database import initialize_database
from autocut_api.jobs import create_queued_job, get_job, list_succeeded_jobs
from autocut_api.local import _copy_completed_result, _submit_auto_upload, _upload_from_terminal
from autocut_api.worker import process_next_job


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), (20, 80, 140)).save(output, format="PNG")
    return output.getvalue()


def test_terminal_menu_lists_completed_jobs_and_saves_result_atomically(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    initialize_database(data_dir)
    job = create_queued_job(data_dir, _png_bytes(), mode="manual")

    assert list_succeeded_jobs(data_dir) == []
    assert process_next_job(data_dir, 100) == {
        "job_id": job["id"],
        "status": "succeeded",
    }

    completed = list_succeeded_jobs(data_dir)
    assert [item["id"] for item in completed] == [job["id"]]
    destination = _copy_completed_result(data_dir, job["id"], tmp_path / "downloads")

    assert destination == tmp_path / "downloads" / f"autocut-{job['id']}.png"
    stored_job = get_job(data_dir, job["id"])
    assert destination.read_bytes() == Path(stored_job["result_path"]).read_bytes()
    assert list((tmp_path / "downloads").glob("*.tmp")) == []


def test_terminal_menu_refuses_unfinished_or_unknown_jobs(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    initialize_database(data_dir)
    job = create_queued_job(data_dir, _png_bytes(), mode="manual")

    with pytest.raises(ValueError, match="unavailable"):
        _copy_completed_result(data_dir, job["id"], tmp_path / "downloads")
    with pytest.raises(ValueError, match="unavailable"):
        _copy_completed_result(data_dir, "unknown", tmp_path / "downloads")
    assert not (tmp_path / "downloads").exists()


def test_terminal_upload_flow_waits_and_saves_completed_png(tmp_path: Path, monkeypatch, capsys) -> None:
    data_dir = tmp_path / "data"
    initialize_database(data_dir)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(_png_bytes())
    job = create_queued_job(data_dir, image_path.read_bytes(), mode="manual")
    process_next_job(data_dir, 100)
    submitted = {}

    def submit(path: Path, content: bytes) -> dict[str, str]:
        submitted["path"] = path
        submitted["bytes"] = content
        return {"job_id": job["id"], "status_url": f"/jobs/{job['id']}"}

    monkeypatch.setattr("builtins.input", lambda _prompt: str(image_path))
    monkeypatch.setattr("autocut_api.local._submit_auto_upload", submit)
    monkeypatch.setattr(
        "autocut_api.local._wait_for_job",
        lambda _url: {"status": "succeeded"},
    )

    downloads_dir = tmp_path / "downloads"
    _upload_from_terminal(data_dir, downloads_dir)

    assert submitted == {"path": image_path, "bytes": image_path.read_bytes()}
    assert (downloads_dir / f"autocut-{job['id']}.png").is_file()
    assert "Selesai. Hasil tersimpan" in capsys.readouterr().out


def test_auto_upload_builds_expected_multipart_request(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "sample.png"
    payload = _png_bytes()
    request_capture = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"job_id":"job-1","status_url":"/jobs/job-1"}'

    def fake_urlopen(request, timeout):
        request_capture["request"] = request
        request_capture["timeout"] = timeout
        return FakeResponse()

    image_path.write_bytes(payload)
    monkeypatch.setattr("autocut_api.local.urlopen", fake_urlopen)

    response = _submit_auto_upload(image_path, payload)
    request = request_capture["request"]

    assert response == {"job_id": "job-1", "status_url": "/jobs/job-1"}
    assert request.full_url == "http://127.0.0.1:8000/jobs"
    assert request.get_method() == "POST"
    assert request_capture["timeout"] == 30
    assert b'name="image"; filename="sample.png"' in request.data
    assert b'name="mode"\r\n\r\nauto' in request.data
    assert payload in request.data
