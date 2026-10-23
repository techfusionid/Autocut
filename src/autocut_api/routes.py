"""HTTP endpoints for job intake and public job resource views."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.responses import Response

from autocut_api.errors import api_error
from autocut_api.jobs import IdempotencyConflict, create_queued_job, get_job
from autocut_api.security import require_bearer_token
from autocut_api.uploads import (
    ImageValidationError,
    LimitedMultiPartParser,
    UploadFileTooLarge,
    media_type_from_verified_bytes,
    validate_image_bytes,
)

router = APIRouter()
_ALLOWED_FIELDS = {
    "image",
    "mode",
    "source_post_url",
    "crop_left",
    "crop_top",
    "crop_right",
    "crop_bottom",
}
_CROP_FIELDS = ("crop_left", "crop_top", "crop_right", "crop_bottom")


@router.post("/jobs", status_code=202)
async def post_job(
    request: Request,
    _authenticated: None = Depends(require_bearer_token),
) -> dict[str, str]:
    settings = request.app.state.settings
    form = None
    try:
        parser = LimitedMultiPartParser(
            request.headers,
            request.stream(),
            max_file_bytes=settings.max_upload_bytes,
        )
        form = await parser.parse()
    except UploadFileTooLarge as exc:
        raise api_error(413, "upload_too_large", "Uploaded file exceeds MAX_UPLOAD_BYTES.") from exc
    except MultiPartException as exc:
        raise api_error(422, "invalid_multipart", "Multipart request is malformed or exceeds field limits.") from exc
    except (KeyError, ValueError) as exc:
        raise api_error(422, "invalid_multipart", "A valid multipart/form-data request is required.") from exc

    try:
        fields = _unique_fields(form.multi_items())
        upload = fields.get("image")
        if not isinstance(upload, UploadFile):
            raise api_error(422, "invalid_input", "The image field must contain an uploaded file.")

        mode = fields.get("mode")
        if not isinstance(mode, str) or mode not in {"manual", "auto"}:
            raise api_error(422, "invalid_input", "mode must be 'manual' or 'auto'.")

        if "source_post_url" in fields and not isinstance(fields["source_post_url"], str):
            raise api_error(422, "invalid_input", "source_post_url must be a text field.")
        source_post_url = fields.get("source_post_url")

        if mode == "auto" and any(name in fields for name in _CROP_FIELDS):
            raise api_error(422, "invalid_input", "Manual crop fields are not allowed in auto mode.")
        crop_values = {
            name: _parse_crop_value(fields.get(name), name)
            for name in _CROP_FIELDS
        }

        if upload.size is not None and upload.size > settings.max_upload_bytes:
            raise api_error(413, "upload_too_large", "Uploaded file exceeds MAX_UPLOAD_BYTES.")
        image_bytes = await upload.read(settings.max_upload_bytes + 1)
        if len(image_bytes) > settings.max_upload_bytes:
            raise api_error(413, "upload_too_large", "Uploaded file exceeds MAX_UPLOAD_BYTES.")

        try:
            validated = await asyncio.to_thread(
                validate_image_bytes,
                image_bytes,
                settings.max_image_pixels,
            )
        except ImageValidationError as exc:
            status_code = 415 if exc.code == "unsupported_image_format" else 422
            raise api_error(status_code, exc.code, exc.message) from exc

        if mode == "manual" and (
            crop_values["crop_left"] + crop_values["crop_right"] >= validated.width
            or crop_values["crop_top"] + crop_values["crop_bottom"] >= validated.height
        ):
            raise api_error(
                422,
                "invalid_crop",
                "Crop margins must leave a positive image width and height after EXIF orientation.",
            )

        try:
            job = await asyncio.to_thread(
                create_queued_job,
                settings.data_dir,
                image_bytes,
                mode=mode,
                source_post_url=source_post_url,
                **crop_values,
                idempotency_key=request.headers.get("Idempotency-Key"),
                input_width=validated.width,
                input_height=validated.height,
            )
        except IdempotencyConflict as exc:
            raise api_error(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request.",
            ) from exc

        return {
            "job_id": job["id"],
            "status": job["status"],
            "status_url": f"/jobs/{job['id']}",
        }
    finally:
        await form.close()


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    request: Request,
    _authenticated: None = Depends(require_bearer_token),
) -> dict[str, Any]:
    job = await asyncio.to_thread(get_job, request.app.state.settings.data_dir, job_id)
    if job is None:
        raise api_error(404, "not_found", "Job not found.")

    return {
        "job_id": job["id"],
        "status": job["status"],
        "mode": job["mode"],
        "source_post_url": job["source_post_url"],
        "original_url": f"/jobs/{job['id']}/original",
        "result_url": f"/jobs/{job['id']}/result",
        "input_size": _size(job["input_width"], job["input_height"]),
        "output_size": _size(job["output_width"], job["output_height"]),
        "crop_box": _crop_box(job),
        "error": _public_error(job),
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
    }


@router.get("/jobs/{job_id}/original")
async def get_original(
    job_id: str,
    request: Request,
    _authenticated: None = Depends(require_bearer_token),
) -> Response:
    job = await asyncio.to_thread(get_job, request.app.state.settings.data_dir, job_id)
    if job is None:
        raise api_error(404, "not_found", "Job not found.")
    try:
        original_bytes = await asyncio.to_thread(Path(job["original_path"]).read_bytes)
    except OSError:
        raise api_error(
            500,
            "original_storage_error",
            "The stored original image is unavailable.",
        ) from None

    media_type = await asyncio.to_thread(media_type_from_verified_bytes, original_bytes)
    if media_type is None:
        raise api_error(
            500,
            "original_storage_error",
            "The stored original image is unavailable.",
        )
    return Response(content=original_bytes, media_type=media_type)


@router.get("/jobs/{job_id}/result")
async def get_result(
    job_id: str,
    request: Request,
    _authenticated: None = Depends(require_bearer_token),
) -> Response:
    job = await asyncio.to_thread(get_job, request.app.state.settings.data_dir, job_id)
    if job is None:
        raise api_error(404, "not_found", "Job not found.")
    if job["status"] != "succeeded":
        raise api_error(409, "result_unavailable", "The processed image is not available yet.")
    if not job["result_path"]:
        raise api_error(
            500,
            "result_storage_error",
            "The processed image is unavailable.",
        )
    try:
        result_bytes = await asyncio.to_thread(Path(job["result_path"]).read_bytes)
    except OSError:
        raise api_error(
            500,
            "result_storage_error",
            "The processed image is unavailable.",
        ) from None
    return Response(content=result_bytes, media_type="image/png")


def _unique_fields(items: list[tuple[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, value in items:
        if name not in _ALLOWED_FIELDS:
            raise api_error(422, "invalid_input", "Unexpected multipart field.")
        if name in fields:
            raise api_error(422, "invalid_input", "Each multipart field must appear only once.")
        fields[name] = value
    return fields


def _parse_crop_value(value: Any, name: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        raise api_error(422, "invalid_input", f"{name} must be a nonnegative integer.")
    normalized = value.strip()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    if not normalized or not normalized.isascii() or not normalized.isdecimal():
        raise api_error(422, "invalid_input", f"{name} must be a nonnegative integer.")
    try:
        return int(normalized)
    except ValueError as exc:
        raise api_error(422, "invalid_input", f"{name} must be a nonnegative integer.") from exc


def _size(width: int | None, height: int | None) -> dict[str, int] | None:
    if width is None or height is None:
        return None
    return {"width": width, "height": height}


def _crop_box(job: dict[str, Any]) -> dict[str, int] | None:
    coordinates = {
        "left": job["crop_box_left"],
        "top": job["crop_box_top"],
        "right": job["crop_box_right"],
        "bottom": job["crop_box_bottom"],
    }
    if any(value is None for value in coordinates.values()):
        return None
    return coordinates


def _public_error(job: dict[str, Any]) -> dict[str, str] | None:
    if job["error_code"] is None and job["error_message"] is None:
        return None
    return {"code": job["error_code"], "message": job["error_message"]}
