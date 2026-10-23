from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from starlette import formparsers
from fastapi.testclient import TestClient
from PIL import Image

from autocut_api.app import create_app
from autocut_api.config import Settings
from autocut_api.database import connect_database, database_path

TOKEN = "api-test-secret"


def make_image(
    image_format: str = "PNG",
    size: tuple[int, int] = (8, 6),
    *,
    orientation: int | None = None,
) -> bytes:
    image = Image.new("RGB", size, (25, 100, 220))
    output = BytesIO()
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    image.save(output, format=image_format, **options)
    return output.getvalue()


def create_client(
    data_dir: Path,
    *,
    max_upload_bytes: int = 15 * 1024 * 1024,
    max_image_pixels: int = 25_000_000,
) -> TestClient:
    settings = Settings(
        api_token=TOKEN,
        data_dir=data_dir,
        max_upload_bytes=max_upload_bytes,
        max_image_pixels=max_image_pixels,
    )
    return TestClient(create_app(settings))


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def upload(
    client: TestClient,
    image_bytes: bytes,
    *,
    mode: str = "manual",
    fields: dict[str, str] | None = None,
    filename: str = "upload.dat",
    content_type: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    data = {"mode": mode, **(fields or {})}
    return client.post(
        "/jobs",
        files={"image": (filename, image_bytes, content_type)},
        data=data,
        headers={**auth_headers(), **(headers or {})},
    )


def assert_no_jobs_or_upload_temps(data_dir: Path) -> None:
    connection = connect_database(database_path(data_dir))
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        connection.close()
    assert list((data_dir / "originals").glob("*.original")) == []
    assert list((data_dir / "originals").glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png")],
)
def test_valid_upload_status_and_exact_original_download(
    tmp_path: Path, image_format: str, media_type: str
) -> None:
    image_bytes = make_image(image_format)
    with create_client(tmp_path) as client:
        response = upload(
            client,
            image_bytes,
            fields={"crop_left": "1", "source_post_url": "https://example.test/p/1"},
        )
        assert response.status_code == 202
        created = response.json()
        assert created["status"] == "queued"
        assert created["status_url"] == f"/jobs/{created['job_id']}"

        status = client.get(created["status_url"], headers=auth_headers())
        assert status.status_code == 200
        job = status.json()
        assert job == {
            "job_id": created["job_id"],
            "status": "queued",
            "mode": "manual",
            "source_post_url": "https://example.test/p/1",
            "original_url": f"/jobs/{created['job_id']}/original",
            "result_url": f"/jobs/{created['job_id']}/result",
            "input_size": {"width": 8, "height": 6},
            "output_size": None,
            "crop_box": None,
            "error": None,
            "created_at": job["created_at"],
            "finished_at": None,
        }
        assert str(tmp_path) not in status.text

        original = client.get(job["original_url"], headers=auth_headers())
        assert original.status_code == 200
        assert original.content == image_bytes
        assert original.headers["content-type"] == media_type

        result = client.get(job["result_url"], headers=auth_headers())
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "result_unavailable"


def test_exif_orientation_dimensions_and_crop_bounds_use_displayed_size(tmp_path: Path) -> None:
    image_bytes = make_image("JPEG", (5, 3), orientation=6)
    with create_client(tmp_path) as client:
        rejected = upload(client, image_bytes, fields={"crop_left": "3"})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "invalid_crop"
        assert_no_jobs_or_upload_temps(tmp_path)

        accepted = upload(client, image_bytes, fields={"crop_left": "2"})
        assert accepted.status_code == 202
        status = client.get(accepted.json()["status_url"], headers=auth_headers())

    assert status.json()["input_size"] == {"width": 3, "height": 5}


@pytest.mark.parametrize("path", ["/jobs", "/jobs/unknown", "/jobs/unknown/original", "/jobs/unknown/result"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic test"])
def test_all_job_endpoints_require_valid_bearer_authentication(
    tmp_path: Path, path: str, authorization: str | None
) -> None:
    with create_client(tmp_path) as client:
        headers = {} if authorization is None else {"Authorization": authorization}
        if path == "/jobs":
            response = client.post(
                path,
                files={"image": ("x.png", make_image(), "image/png")},
                data={"mode": "manual"},
                headers=headers,
            )
        else:
            response = client.get(path, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert_no_jobs_or_upload_temps(tmp_path)


def test_mode_is_required(tmp_path: Path) -> None:
    with create_client(tmp_path) as client:
        response = client.post(
            "/jobs",
            files={"image": ("image.png", make_image(), "image/png")},
            headers=auth_headers(),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    assert_no_jobs_or_upload_temps(tmp_path)


def test_rejected_upload_closes_multipart_temporary_file(tmp_path: Path, monkeypatch) -> None:
    original_factory = formparsers.SpooledTemporaryFile
    temporary_files = []

    def track_spooled_file(*args, **kwargs):
        temporary_file = original_factory(*args, **kwargs)
        temporary_files.append(temporary_file)
        return temporary_file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", track_spooled_file)
    with create_client(tmp_path) as client:
        response = upload(client, b"corrupted image bytes")

    assert response.status_code == 422
    assert temporary_files
    assert all(temporary_file.closed for temporary_file in temporary_files)
    assert_no_jobs_or_upload_temps(tmp_path)


def test_corrupt_unsupported_animated_and_excessive_pixel_images_are_rejected(tmp_path: Path) -> None:
    corrupted = b"not an image"
    unsupported = make_image("GIF", (2, 2))
    frame = Image.new("RGB", (4, 4), "red")
    other_frame = Image.new("RGB", (4, 4), "blue")
    animated_out = BytesIO()
    frame.save(animated_out, format="GIF", save_all=True, append_images=[other_frame], duration=10)
    oversized_pixels = make_image("PNG", (4, 4))

    with create_client(tmp_path, max_image_pixels=100) as client:
        corrupt_response = upload(client, corrupted)
        unsupported_response = upload(client, unsupported)
        animated_response = upload(client, animated_out.getvalue())
    with create_client(tmp_path, max_image_pixels=10) as client:
        pixel_response = upload(client, oversized_pixels)

    assert corrupt_response.status_code == 422
    assert corrupt_response.json()["error"]["code"] == "invalid_image"
    assert unsupported_response.status_code == 415
    assert unsupported_response.json()["error"]["code"] == "unsupported_image_format"
    assert animated_response.status_code == 422
    assert animated_response.json()["error"]["code"] == "animated_image_unsupported"
    assert pixel_response.status_code == 422
    assert pixel_response.json()["error"]["code"] == "image_too_large"
    assert_no_jobs_or_upload_temps(tmp_path)


@pytest.mark.parametrize(
    ("mode", "fields"),
    [
        ("unknown", {}),
        ("manual", {"crop_left": "-1"}),
        ("manual", {"crop_top": "one"}),
        ("manual", {"crop_right": "1.5"}),
        ("manual", {"crop_bottom": "9" * 5000}),
        ("auto", {"crop_left": "0"}),
    ],
)
def test_invalid_mode_and_crop_parameters_are_rejected(
    tmp_path: Path, mode: str, fields: dict[str, str]
) -> None:
    with create_client(tmp_path) as client:
        response = upload(client, make_image(), mode=mode, fields=fields)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    assert_no_jobs_or_upload_temps(tmp_path)


def test_automatic_mode_queues_only_after_valid_image_validation(tmp_path: Path) -> None:
    with create_client(tmp_path) as client:
        invalid_image = upload(client, b"bad", mode="auto")
        valid_image = upload(client, make_image(), mode="auto")

    assert invalid_image.status_code == 422
    assert invalid_image.json()["error"]["code"] == "invalid_image"
    assert valid_image.status_code == 202
    assert valid_image.json()["status"] == "queued"


def test_file_size_limit_is_enforced_while_parsing_without_content_length(tmp_path: Path) -> None:
    max_file_bytes = len(make_image()) - 1
    image_bytes = make_image()
    boundary = b"stage2b-boundary"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="mode"\r\n\r\nmanual\r\n'
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="image"; filename="image.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        + image_bytes
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )

    async def send_without_content_length() -> httpx.Response:
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body[:17]
                yield body[17:]

        app = create_client(tmp_path, max_upload_bytes=max_file_bytes).app
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/jobs",
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                    },
                    content=Stream(),
                )

    response = asyncio.run(send_without_content_length())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert_no_jobs_or_upload_temps(tmp_path)


def test_total_multipart_overhead_limit_is_enforced(tmp_path: Path) -> None:
    with create_client(tmp_path, max_upload_bytes=1024) as client:
        response = client.post(
            "/jobs",
            files={"image": ("image.png", make_image(), "image/png")},
            data={
                "mode": "manual",
                "source_post_url": "x" * 40_000,
                "crop_left": "1" * 10_000,
                "crop_top": "1" * 10_000,
                "crop_right": "1" * 10_000,
            },
            headers=auth_headers(),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert_no_jobs_or_upload_temps(tmp_path)


def test_matching_and_conflicting_idempotency_keys(tmp_path: Path) -> None:
    image_bytes = make_image()
    with create_client(tmp_path) as client:
        first = upload(client, image_bytes, fields={"crop_left": "1"}, headers={"Idempotency-Key": "retry-1"})
        retry = upload(client, image_bytes, fields={"crop_left": "1"}, headers={"Idempotency-Key": "retry-1"})
        conflict = upload(client, image_bytes, fields={"crop_left": "2"}, headers={"Idempotency-Key": "retry-1"})

    assert first.status_code == retry.status_code == 202
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    connection = connect_database(database_path(tmp_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        connection.close()
    assert len(list((tmp_path / "originals").glob("*.original"))) == 1
    assert list((tmp_path / "originals").glob("*.tmp")) == []


def test_unknown_jobs_and_missing_original_have_structured_errors(tmp_path: Path) -> None:
    with create_client(tmp_path) as client:
        unknown_status = client.get("/jobs/no-such-job", headers=auth_headers())
        unknown_original = client.get("/jobs/no-such-job/original", headers=auth_headers())
        unknown_result = client.get("/jobs/no-such-job/result", headers=auth_headers())
        created = upload(client, make_image())
        job_id = created.json()["job_id"]
        connection = connect_database(database_path(tmp_path))
        try:
            path = connection.execute("SELECT original_path FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        finally:
            connection.close()
        Path(path).unlink()
        missing_original = client.get(f"/jobs/{job_id}/original", headers=auth_headers())

    assert unknown_status.status_code == unknown_original.status_code == unknown_result.status_code == 404
    assert missing_original.status_code == 500
    assert missing_original.json() == {
        "error": {
            "code": "original_storage_error",
            "message": "The stored original image is unavailable.",
        }
    }
    assert str(tmp_path) not in missing_original.text
