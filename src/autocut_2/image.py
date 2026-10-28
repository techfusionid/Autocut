import io

import httpx
import numpy as np
from PIL import Image

FRAME_COLOR = (0, 0, 63)  # biru navy frame, hasil sampling dari gambar asli
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class PosterNotFound(ValueError):
    """Area poster tidak terdeteksi pada gambar."""


class ImageTooLarge(ValueError):
    """Gambar sumber melebihi batas ukuran."""


class InvalidImage(ValueError):
    """Data yang diterima bukan gambar yang bisa didekode."""


def decode_image(data: bytes) -> Image.Image:
    """Decode bytes gambar ke RGB."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise InvalidImage("data is not a decodable image") from exc
    return img.convert("RGB")


async def load_image_from_url(client: httpx.AsyncClient, url: str) -> Image.Image:
    """Ambil gambar dari URL, batasi ukurannya, lalu decode ke RGB."""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        length = resp.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_IMAGE_BYTES:
            raise ImageTooLarge(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_IMAGE_BYTES:
                raise ImageTooLarge(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    return decode_image(bytes(buf))


def crop_poster(img: Image.Image, border_color=FRAME_COLOR, tol: int = 18,
                min_ratio: float = 0.8, pad: int = 0) -> Image.Image:
    arr = np.asarray(img).astype(int)
    bc = np.array(border_color)
    is_content = np.abs(arr - bc).max(axis=2) > tol

    rows = np.where(is_content.mean(axis=1) > min_ratio)[0]
    cols = np.where(is_content.mean(axis=0) > min_ratio)[0]
    if rows.size == 0 or cols.size == 0:
        raise PosterNotFound("Poster area not found")

    box = (int(cols[0]) + pad, int(rows[0]) + pad,
           int(cols[-1]) + 1 - pad, int(rows[-1]) + 1 - pad)
    return img.crop(box)


def encode(image: Image.Image, fmt: str = "PNG") -> tuple[bytes, str]:
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue(), f"image/{fmt.lower()}"


def crop_poster_from_bytes(data: bytes, *, border_color=FRAME_COLOR, tol: int = 18,
                           min_ratio: float = 0.8, pad: int = 0,
                           fmt: str = "PNG") -> tuple[bytes, str]:
    return encode(crop_poster(decode_image(data), border_color, tol, min_ratio, pad), fmt)


async def crop_poster_from_url(client: httpx.AsyncClient, url: str, *,
                               border_color=FRAME_COLOR, tol: int = 18,
                               min_ratio: float = 0.8, pad: int = 0,
                               fmt: str = "PNG") -> tuple[bytes, str]:
    img = await load_image_from_url(client, url)
    return encode(crop_poster(img, border_color, tol, min_ratio, pad), fmt)
