"""Synchronous image-cropping API. No jobs, database, or worker required."""

from __future__ import annotations

import asyncio
import os
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.datastructures import Headers, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse, Response

from autocut_api.detection import FrameDetectionUncertain, detect_frame_box
from autocut_api.fetch import FetchError, fetch_image_url

MULTIPART_OVERHEAD_LIMIT = 64 * 1024
MAX_FORM_FIELD_BYTES = 64 * 1024
SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_FIELDS = {"image", "image_url", "mode", "crop_left", "crop_top", "crop_right", "crop_bottom"}
_CROP_FIELDS = ("crop_left", "crop_top", "crop_right", "crop_bottom")


def api_error(status: int, code: str, message: str) -> StarletteHTTPException:
    return StarletteHTTPException(status, detail={"code": code, "message": message})


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {
                "code": detail.get("code", "http_error"),
                "message": detail.get("message", "Request failed."),
            }},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "Request validation failed."}},
        )


@dataclass(frozen=True)
class Settings:
    max_upload_bytes: int = 15 * 1024 * 1024
    max_image_pixels: int = 25_000_000
    image_fetch_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            max_upload_bytes=_positive_int("MAX_UPLOAD_BYTES", 15 * 1024 * 1024),
            max_image_pixels=_positive_int("MAX_IMAGE_PIXELS", 25_000_000),
            image_fetch_timeout=_positive_float("IMAGE_FETCH_TIMEOUT", 15.0),
        )


class ImageError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class UploadTooLarge(MultiPartException):
    def __init__(self) -> None:
        super().__init__("Uploaded file or request is too large.")


class LimitedParser(MultiPartParser):
    """Reject a file as soon as its multipart part exceeds the configured limit."""

    def __init__(self, headers: Headers, stream: Any, max_file_bytes: int) -> None:
        super().__init__(headers, stream, max_files=1, max_fields=len(_FIELDS), max_part_size=MAX_FORM_FIELD_BYTES)
        self.max_file_bytes = max_file_bytes
        self._file_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._file_bytes += end - start
            if self._file_bytes > self.max_file_bytes:
                raise UploadTooLarge
        super().on_part_data(data, start, end)


class BodyLimitMiddleware:
    """Bound multipart framing and form fields in addition to the image file."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/crop" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        application = scope.get("app")
        settings = getattr(getattr(application, "state", None), "settings", Settings())
        limit = settings.max_upload_bytes + MULTIPART_OVERHEAD_LIMIT
        total = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise UploadTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except UploadTooLarge:
            response = JSONResponse(
                status_code=413,
                content={"error": {"code": "upload_too_large", "message": "Upload exceeds the configured size limit."}},
            )
            await response(scope, receive, send)


def crop_image_bytes(
    payload: bytes,
    *,
    mode: str = "auto",
    margins: tuple[int, int, int, int] = (0, 0, 0, 0),
    max_image_pixels: int = 25_000_000,
) -> bytes:
    """Validate, orient, crop, and return one PNG image."""
    if mode not in {"auto", "manual"}:
        raise ImageError("invalid_mode", "mode must be 'auto' or 'manual'.")
    width, height = _validate_image(payload, max_image_pixels)
    try:
        with Image.open(BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.size != (width, height):
                raise ImageError("invalid_image", "Image dimensions could not be validated.")
            if mode == "auto":
                try:
                    box = detect_frame_box(image)
                except FrameDetectionUncertain as exc:
                    raise ImageError("frame_uncertain", "A clear crop boundary could not be identified.") from exc
            else:
                left_margin, top_margin, right_margin, bottom_margin = margins
                if any(not isinstance(value, int) or value < 0 for value in margins):
                    raise ImageError("invalid_crop", "Crop margins must be nonnegative integers.")
                box = (left_margin, top_margin, width - right_margin, height - bottom_margin)
                if box[0] >= box[2] or box[1] >= box[3]:
                    raise ImageError("invalid_crop", "Crop margins must leave positive width and height.")
            cropped = image.crop(box)
            if cropped.mode == "CMYK":
                png = cropped.convert("RGB")
            elif cropped.mode in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
                png = cropped
            else:
                has_alpha = "A" in cropped.getbands() or (
                    cropped.mode == "P" and "transparency" in cropped.info
                )
                png = cropped.convert("RGBA" if has_alpha else "RGB")
            output = BytesIO()
            png.save(output, format="PNG")
            if png is not cropped:
                png.close()
            cropped.close()
            return output.getvalue()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, TypeError, EOFError) as exc:
        raise ImageError("invalid_image", "Image is malformed or could not be decoded.") from exc


def _validate_image(payload: bytes, max_image_pixels: int) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as probe:
                file_format = probe.format
                _check_pixels(probe.width, probe.height, max_image_pixels)
                if getattr(probe, "is_animated", False) or getattr(probe, "n_frames", 1) > 1:
                    raise ImageError("animated_image_unsupported", "Animated images are not supported.")
                if file_format not in SUPPORTED_FORMATS:
                    raise ImageError(
                        "unsupported_image_format",
                        "Only static JPEG, PNG, and WebP images are supported.",
                        415,
                    )
                probe.verify()
            with Image.open(BytesIO(payload)) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                _check_pixels(*oriented.size, max_image_pixels)
                return oriented.size
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageError("image_too_large", "Image exceeds the decoded pixel limit.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, TypeError, EOFError) as exc:
        raise ImageError("invalid_image", "Image is malformed or could not be decoded.") from exc


def _check_pixels(width: int, height: int, limit: int) -> None:
    if width <= 0 or height <= 0 or width * height > limit:
        raise ImageError("image_too_large", "Image exceeds the decoded pixel limit.")


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if not 0.1 <= value <= 120:
        raise ValueError(f"{name} must be a positive number.")
    return value


def _parse_margins(fields: dict[str, Any]) -> tuple[int, int, int, int]:
    values: list[int] = []
    for name in _CROP_FIELDS:
        raw = fields.get(name)
        if raw is None:
            values.append(0)
        elif not isinstance(raw, str) or not raw.strip().isascii() or not raw.strip().isdecimal():
            raise ImageError("invalid_crop", f"{name} must be a nonnegative integer.")
        else:
            values.append(int(raw.strip()))
    return tuple(values)  # type: ignore[return-value]


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Autocut")
    app.state.settings = settings or Settings.from_env()
    app.add_middleware(BodyLimitMiddleware)
    _install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "autocut"}

    @app.post("/crop")
    async def crop(request: Request) -> Response:
        parser = LimitedParser(request.headers, request.stream(), app.state.settings.max_upload_bytes)
        try:
            form = await parser.parse()
        except UploadTooLarge as exc:
            raise api_error(413, "upload_too_large", "Upload exceeds the configured file-size limit.") from exc
        except (MultiPartException, KeyError, ValueError) as exc:
            raise api_error(422, "invalid_multipart", "A valid multipart/form-data request is required.") from exc
        try:
            fields: dict[str, Any] = {}
            for name, value in form.multi_items():
                if name not in _FIELDS or name in fields:
                    raise api_error(422, "invalid_input", "Unexpected or repeated multipart field.")
                fields[name] = value
            upload = fields.get("image")
            remote_url = fields.get("image_url")
            if upload is not None and remote_url is not None:
                raise api_error(422, "invalid_input", "Send either image or image_url, not both.")
            if upload is None and remote_url is None:
                raise api_error(422, "invalid_input", "Provide an image file or an image_url.")
            if upload is not None and not isinstance(upload, UploadFile):
                raise api_error(422, "invalid_input", "The image field must contain a file.")
            if remote_url is not None and not isinstance(remote_url, str):
                raise api_error(422, "invalid_url", "image_url must be a valid http or https URL.")
            mode = fields.get("mode", "auto")
            if not isinstance(mode, str) or mode not in {"auto", "manual"}:
                raise api_error(422, "invalid_mode", "mode must be 'auto' or 'manual'.")
            if mode == "auto" and any(name in fields for name in _CROP_FIELDS):
                raise api_error(422, "invalid_input", "Crop margins are only valid in manual mode.")
            margins = _parse_margins(fields)
            if upload is not None:
                payload = await upload.read(app.state.settings.max_upload_bytes + 1)
                if len(payload) > app.state.settings.max_upload_bytes:
                    raise api_error(413, "upload_too_large", "Upload exceeds the configured file-size limit.")
            else:
                try:
                    payload = await fetch_image_url(
                        remote_url,
                        max_bytes=app.state.settings.max_upload_bytes,
                        timeout=app.state.settings.image_fetch_timeout,
                    )
                except FetchError as exc:
                    raise api_error(exc.status_code, exc.code, exc.message) from exc
            try:
                result = await asyncio.to_thread(
                    crop_image_bytes,
                    payload,
                    mode=mode,
                    margins=margins,
                    max_image_pixels=app.state.settings.max_image_pixels,
                )
            except ImageError as exc:
                raise api_error(exc.status_code, exc.code, exc.message) from exc
            return Response(result, media_type="image/png")
        finally:
            await form.close()

    return app
