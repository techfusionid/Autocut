import base64
import io
import json
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .image import (
    ImageTooLarge,
    InvalidImage,
    PosterNotFound,
    crop_poster_from_bytes,
    crop_poster_from_url,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="autocut-2", version="0.1.0", lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


class CropOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    border_color: tuple[int, int, int] = (0, 0, 63)
    tol: int = Field(18, ge=0, le=255)
    min_ratio: float = Field(0.8, gt=0, le=1)
    pad: int = Field(0, ge=0)
    format: str = Field("png", pattern="^(png|jpeg)$")

    @field_validator("border_color")
    @classmethod
    def _check_channels(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(not 0 <= c <= 255 for c in value):
            raise ValueError("border_color channels must be within 0-255")
        return value


def _options(border_color: str, tol: int, min_ratio: float, pad: int, fmt: str) -> CropOptions:
    raw = border_color.strip()
    try:
        parts = json.loads(raw) if raw.startswith("[") else [int(p) for p in raw.split(",")]
    except ValueError as exc:
        raise HTTPException(422, "border_color must be 'r,g,b'") from exc
    if len(parts) != 3:
        raise HTTPException(422, "border_color must have 3 components")
    try:
        return CropOptions(border_color=tuple(parts), tol=tol, min_ratio=min_ratio,
                           pad=pad, format=fmt)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ImageTooLarge):
        return HTTPException(413, str(exc))
    if isinstance(exc, PosterNotFound):
        return HTTPException(422, str(exc))
    if isinstance(exc, InvalidImage):
        return HTTPException(415, str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(502, f"upstream returned {exc.response.status_code}")
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, "upstream timeout")
    if isinstance(exc, httpx.HTTPError):
        return HTTPException(502, "upstream fetch failed")
    if isinstance(exc, ValueError):
        return HTTPException(415, str(exc))
    raise exc


async def _read_upload(file: UploadFile) -> bytes:
    buf = bytearray()
    while chunk := await file.read(1024 * 1024):
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"image exceeds {MAX_UPLOAD_BYTES} bytes")
    return bytes(buf)


async def _crop(request: Request, url: str | None, file: UploadFile | None,
                opts: CropOptions) -> tuple[bytes, str]:
    fmt = "JPEG" if opts.format == "jpeg" else "PNG"
    try:
        if url:
            return await crop_poster_from_url(
                request.app.state.client, url,
                border_color=tuple(opts.border_color), tol=opts.tol,
                min_ratio=opts.min_ratio, pad=opts.pad, fmt=fmt,
            )
        data = await _read_upload(file)
        return crop_poster_from_bytes(
            data, border_color=tuple(opts.border_color), tol=opts.tol,
            min_ratio=opts.min_ratio, pad=opts.pad, fmt=fmt,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post(
    "/v1/crop",
    response_class=Response,
    responses={
        200: {"description": "Cropped poster image",
              "content": {"image/png": {}, "image/jpeg": {}}},
        413: {"description": "Source image too large"},
        415: {"description": "Source is not an image"},
        422: {"description": "Poster area not found or invalid parameters"},
        502: {"description": "Upstream fetch failed"},
        504: {"description": "Upstream timeout"},
    },
)
async def crop_poster(
    request: Request,
    url: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    border_color: Annotated[str, Form()] = "0,0,63",
    tol: Annotated[int, Form()] = 18,
    min_ratio: Annotated[float, Form()] = 0.8,
    pad: Annotated[int, Form()] = 0,
    format: Annotated[str, Form()] = "png",
    meta: Annotated[bool, Form()] = False,
) -> Response:
    """Crop poster dari URL atau file upload.

    Set `meta=true` untuk menerima JSON {width, height, content_type, image_base64}
    alih-alih binary image.
    """
    if not url and file is None:
        raise HTTPException(422, "provide either 'url' or 'file'")

    opts = _options(border_color, tol, min_ratio, pad, format)
    data, content_type = await _crop(request, url, file, opts)

    if meta:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        body = {"width": width, "height": height, "content_type": content_type,
                "image_base64": base64.b64encode(data).decode()}
        return Response(content=json.dumps(body), media_type="application/json")

    return Response(content=data, media_type=content_type)
