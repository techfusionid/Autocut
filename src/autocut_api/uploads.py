"""Bounded multipart intake and static JPEG/PNG validation."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

MULTIPART_OVERHEAD_LIMIT = 64 * 1024
MAX_FORM_FIELDS = 6  # mode, source_post_url, and up to four crop margins
MAX_FORM_FIELD_BYTES = 64 * 1024


class RequestBodyTooLarge(Exception):
    """Raised when total multipart body bytes exceed file limit plus overhead."""


class UploadFileTooLarge(Exception):
    """Raised as soon as a multipart file part exceeds MAX_UPLOAD_BYTES."""


class ImageValidationError(Exception):
    code = "invalid_image"
    message = "Image is malformed or could not be decoded."


class UnsupportedImageFormat(ImageValidationError):
    code = "unsupported_image_format"
    message = "Only static JPEG and PNG images are supported."


class AnimatedImage(ImageValidationError):
    code = "animated_image_unsupported"
    message = "Animated images are not supported."


class ImagePixelLimitExceeded(ImageValidationError):
    code = "image_too_large"
    message = "Decoded image dimensions exceed MAX_IMAGE_PIXELS."


@dataclass(frozen=True)
class ValidatedImage:
    media_type: str
    width: int
    height: int


class RequestBodyLimitMiddleware:
    """Cap total multipart bytes on /jobs while ASGI receives the body."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/jobs"
        ):
            await self.app(scope, receive, send)
            return

        application = scope.get("app")
        settings = getattr(getattr(application, "state", None), "settings", None)
        if settings is None:
            await self.app(scope, receive, send)
            return

        max_body_bytes = settings.max_upload_bytes + MULTIPART_OVERHEAD_LIMIT
        received_bytes = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_body_bytes:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": "Multipart request exceeds MAX_UPLOAD_BYTES plus the 64 KiB request overhead limit.",
                }
            },
        )
        await response(scope, receive, send)


class LimitedMultiPartParser(MultiPartParser):
    """Starlette parser that rejects oversized file parts before spooling them."""

    def __init__(self, headers: Headers, stream: Any, *, max_file_bytes: int) -> None:
        super().__init__(
            headers,
            stream,
            max_files=1,
            max_fields=MAX_FORM_FIELDS,
            max_part_size=MAX_FORM_FIELD_BYTES,
        )
        self.max_file_bytes = max_file_bytes
        self._file_bytes_seen = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._file_bytes_seen = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._file_bytes_seen += end - start
            if self._file_bytes_seen > self.max_file_bytes:
                raise UploadFileTooLarge
        super().on_part_data(data, start, end)


def validate_image_bytes(image_bytes: bytes, max_image_pixels: int) -> ValidatedImage:
    """Verify a static JPEG/PNG and return displayed dimensions after EXIF orientation."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as probe:
                file_format = probe.format
                _check_dimensions(probe.width, probe.height, max_image_pixels)
                if getattr(probe, "is_animated", False) or getattr(probe, "n_frames", 1) > 1:
                    raise AnimatedImage
                if file_format not in {"JPEG", "PNG"}:
                    raise UnsupportedImageFormat
                probe.verify()

            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
                displayed = ImageOps.exif_transpose(image)
                width, height = displayed.size
                _check_dimensions(width, height, max_image_pixels)
        return ValidatedImage(
            media_type="image/jpeg" if file_format == "JPEG" else "image/png",
            width=width,
            height=height,
        )
    except (UnsupportedImageFormat, AnimatedImage, ImagePixelLimitExceeded):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImagePixelLimitExceeded from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, TypeError, EOFError) as exc:
        raise ImageValidationError from exc


def _check_dimensions(width: int, height: int, max_image_pixels: int) -> None:
    if width <= 0 or height <= 0 or width * height > max_image_pixels:
        raise ImagePixelLimitExceeded


def media_type_from_verified_bytes(image_bytes: bytes) -> str | None:
    """Identify the header format of a previously verified stored original."""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            return {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
            }.get(image.format)
    except (UnidentifiedImageError, OSError, ValueError):
        return None


