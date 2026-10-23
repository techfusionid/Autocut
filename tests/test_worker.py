from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from autocut_api.app import create_app
from autocut_api.config import Settings
from autocut_api.database import connect_database, database_path, initialize_database
from autocut_api.jobs import claim_next_job, create_queued_job, get_job
from autocut_api.worker import process_next_job, run_worker

TOKEN = "stage3-test-token"


def rgba_fixture() -> tuple[bytes, list[list[tuple[int, int, int, int]]]]:
    pixels = [
        [(255, 0, 0, 0), (0, 255, 0, 40), (0, 0, 255, 80)],
        [(255, 255, 0, 120), (255, 0, 255, 160), (0, 255, 255, 200)],
    ]
    image = Image.new("RGBA", (3, 2))
    for y, row in enumerate(pixels):
        for x, value in enumerate(row):
            image.putpixel((x, y), value)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), pixels


def enqueue(data_dir: Path, payload: bytes, **kwargs) -> dict:
    initialize_database(data_dir)
    return create_queued_job(data_dir, payload, mode="manual", **kwargs)


def test_manual_crop_pixels_dimensions_and_original_bytes_are_preserved(tmp_path: Path) -> None:
    payload, pixels = rgba_fixture()
    job = enqueue(tmp_path, payload, crop_left=1, crop_top=0, crop_right=1, crop_bottom=1)

    outcome = process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])
    result = Image.open(stored["result_path"])

    assert outcome == {"job_id": job["id"], "status": "succeeded"}
    assert stored["status"] == "succeeded"
    assert stored["attempt_count"] == 1
    assert stored["input_width"] == 3 and stored["input_height"] == 2
    assert stored["output_width"] == 1 and stored["output_height"] == 1
    assert (stored["crop_box_left"], stored["crop_box_top"], stored["crop_box_right"], stored["crop_box_bottom"]) == (1, 0, 2, 1)
    assert result.mode == "RGBA"
    assert result.getpixel((0, 0)) == pixels[0][1]
    assert Path(stored["original_path"]).read_bytes() == payload


def test_zero_margins_produce_full_image_and_preserve_transparency(tmp_path: Path) -> None:
    payload, pixels = rgba_fixture()
    job = enqueue(tmp_path, payload)
    process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])
    with Image.open(stored["result_path"]) as result:
        assert result.size == (3, 2)
        assert result.mode == "RGBA"
        assert [[result.getpixel((x, y)) for x in range(3)] for y in range(2)] == pixels


def test_exif_orientation_is_applied_before_crop_coordinates(tmp_path: Path) -> None:
    image = Image.new("RGB", (3, 2))
    letters = {
        (0, 0): (255, 0, 0), (1, 0): (0, 255, 0), (2, 0): (0, 0, 255),
        (0, 1): (255, 255, 0), (1, 1): (255, 0, 255), (2, 1): (0, 255, 255),
    }
    for point, color in letters.items():
        image.putpixel(point, color)
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    image.save(output, format="PNG", exif=exif)
    payload = output.getvalue()
    job = enqueue(tmp_path, payload, crop_left=1, crop_top=1, crop_bottom=1)

    process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])

    assert (stored["input_width"], stored["input_height"]) == (2, 3)
    assert (stored["output_width"], stored["output_height"]) == (1, 1)
    assert (stored["crop_box_left"], stored["crop_box_top"], stored["crop_box_right"], stored["crop_box_bottom"]) == (1, 1, 2, 2)
    with Image.open(stored["result_path"]) as result:
        assert result.getpixel((0, 0)) == (0, 255, 0)


def test_cmyk_jpeg_is_converted_to_png_compatible_rgb(tmp_path: Path) -> None:
    image = Image.new("CMYK", (4, 3), (10, 80, 150, 20))
    output = BytesIO()
    image.save(output, format="JPEG")
    job = enqueue(tmp_path, output.getvalue(), crop_right=1)

    process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])
    with Image.open(stored["result_path"]) as result:
        assert result.format == "PNG"
        assert result.mode == "RGB"
        assert result.size == (3, 3)


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_corrupt_original_transitions_to_failed(tmp_path: Path, missing: bool) -> None:
    job = enqueue(tmp_path, b"not a valid image")
    if missing:
        Path(job["original_path"]).unlink()

    outcome = process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])

    assert outcome["status"] == stored["status"] == "failed"
    assert stored["finished_at"] is not None
    assert stored["result_path"] is None
    assert stored["error_code"] == ("original_missing" if missing else "invalid_image")
    assert stored["error_message"]
    assert not list((tmp_path / "results").glob("*.png")) if (tmp_path / "results").exists() else True


def test_invalid_stored_crop_is_failed_without_result(tmp_path: Path) -> None:
    payload, _ = rgba_fixture()
    job = enqueue(tmp_path, payload, crop_left=1)
    connection = connect_database(database_path(tmp_path))
    try:
        connection.execute("UPDATE jobs SET crop_left = 3 WHERE id = ?", (job["id"],))
    finally:
        connection.close()

    process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "invalid_crop"
    assert stored["result_path"] is None


def test_atomic_claims_with_separate_database_connections(tmp_path: Path) -> None:
    job = enqueue(tmp_path, rgba_fixture()[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda name: claim_next_job(tmp_path, name), ["worker-a", "worker-b"]))

    successful_claims = [item for item in claimed if item is not None]
    assert len(successful_claims) == 1
    assert successful_claims[0]["id"] == job["id"]
    assert successful_claims[0]["status"] == "processing"
    assert successful_claims[0]["attempt_count"] == 1
    assert get_job(tmp_path, job["id"])["status"] == "processing"


def test_result_write_failure_marks_failed_and_leaves_no_result_or_temporary_file(
    tmp_path: Path, monkeypatch
) -> None:
    job = enqueue(tmp_path, rgba_fixture()[0])

    def fail_save(image: Image.Image, fp, **kwargs) -> None:
        fp.write(b"partial")
        raise OSError("simulated result storage failure")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    outcome = process_next_job(tmp_path, 100)
    stored = get_job(tmp_path, job["id"])

    assert outcome["status"] == stored["status"] == "failed"
    assert stored["error_code"] == "result_write_failed"
    assert stored["result_path"] is None
    assert list((tmp_path / "results").glob("*.png")) == []
    assert list((tmp_path / "results").glob("*.tmp")) == []


def test_worker_once_processes_at_most_one_job_and_empty_queue_exits(tmp_path: Path) -> None:
    payload, _ = rgba_fixture()
    first = enqueue(tmp_path, payload)
    second = create_queued_job(tmp_path, payload, mode="manual")

    assert run_worker(tmp_path, 100, once=True) == 1
    statuses = {job_id: get_job(tmp_path, job_id)["status"] for job_id in (first["id"], second["id"])}
    assert sorted(statuses.values()) == ["queued", "succeeded"]
    assert run_worker(tmp_path, 100, once=True) == 1
    assert run_worker(tmp_path, 100, once=True) == 0


def test_succeeded_result_endpoint_serves_png_and_missing_result_is_structured(tmp_path: Path) -> None:
    payload, _ = rgba_fixture()
    job = enqueue(tmp_path, payload, crop_left=1)
    process_next_job(tmp_path, 100)
    settings = Settings(api_token=TOKEN, data_dir=tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(settings)) as client:
        status = client.get(f"/jobs/{job['id']}", headers=headers)
        assert status.status_code == 200
        public_job = status.json()
        assert public_job["status"] == "succeeded"
        assert public_job["input_size"] == {"width": 3, "height": 2}
        assert public_job["output_size"] == {"width": 2, "height": 2}
        assert public_job["crop_box"] == {"left": 1, "top": 0, "right": 3, "bottom": 2}
        assert public_job["error"] is None
        result = client.get(f"/jobs/{job['id']}/result", headers=headers)
        assert result.status_code == 200
        assert result.headers["content-type"] == "image/png"
        with Image.open(BytesIO(result.content)) as image:
            assert image.size == (2, 2)
        stored = get_job(tmp_path, job["id"])
        Path(stored["result_path"]).unlink()
        missing = client.get(f"/jobs/{job['id']}/result", headers=headers)
        assert missing.status_code == 500
        assert missing.json() == {
            "error": {
                "code": "result_storage_error",
                "message": "The processed image is unavailable.",
            }
        }
        assert str(tmp_path) not in missing.text
        connection = connect_database(database_path(tmp_path))
        try:
            connection.execute("UPDATE jobs SET result_path = NULL WHERE id = ?", (job["id"],))
        finally:
            connection.close()
        missing_path = client.get(f"/jobs/{job['id']}/result", headers=headers)
        assert missing_path.status_code == 500
        assert missing_path.json()["error"]["code"] == "result_storage_error"
