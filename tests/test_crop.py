"""Test crop poster terhadap aset nyata: file lokal (PNG/WebP) dan URL CDN.

Setiap hasil crop disimpan ke `outputs/` (dan perbandingan side-by-side sumber vs
hasil) supaya bisa dilihat langsung tanpa harus memakai API.
"""
import base64
import io
import re
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from autocut_2.api import app
from autocut_2.image import FRAME_COLOR

TESTS_DIR = Path(__file__).parent
OUTPUTS_DIR = Path(__file__).parent.parent / "outputs"
IMAGE_FILES = sorted(p for p in TESTS_DIR.iterdir()
                     if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})

URL_FILE = TESTS_DIR / "url.txt"
REAL_URLS: list[str] = []
if URL_FILE.exists():
    REAL_URLS = re.findall(r'=\s*"(https://[^"]+)"', URL_FILE.read_text())


@pytest.fixture(scope="session")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def outputs_dir():
    OUTPUTS_DIR.mkdir(exist_ok=True)
    return OUTPUTS_DIR


def navy_edges(arr: np.ndarray) -> float:
    """Porsi piksel solid-navy pada empat tepi gambar (harus ~0 setelah crop)."""
    navy = np.abs(arr - np.array(FRAME_COLOR)).max(axis=2) <= 18
    edges = np.concatenate([navy[0], navy[-1], navy[:, 0], navy[:, -1]])
    return float(edges.mean())


def save_comparison(name: str, source: Image.Image, cropped: Image.Image) -> Path:
    """Simpan perbandingan sumber | hasil, tinggi sama, ke outputs/."""
    height = max(source.height, cropped.height)
    canvas = Image.new("RGB", (source.width + cropped.width + 24, height), (24, 24, 24))
    canvas.paste(source, (0, 0))
    canvas.paste(cropped, (source.width + 24, 0))
    target = OUTPUTS_DIR / f"compare_{name}.png"
    canvas.save(target)
    return target


@pytest.mark.parametrize("path", IMAGE_FILES, ids=lambda p: p.name)
def test_crop_local_file(client, path, outputs_dir):
    with path.open("rb") as fh:
        r = client.post("/v1/crop", files={"file": (path.name, fh, "application/octet-stream")})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"

    out = Image.open(io.BytesIO(r.content)).convert("RGB")
    source = Image.open(path).convert("RGB")
    assert out.width <= source.width and out.height <= source.height
    # matte navy solid harus hilang dari tepi hasil crop
    assert navy_edges(np.asarray(out)) < 0.2

    stem = path.stem.replace(" ", "_").replace("(", "").replace(")", "")
    (outputs_dir / f"file_{stem}.png").write_bytes(r.content)
    save_comparison(f"file_{stem}", source, out)
    print(f"\n[output] {outputs_dir / f'file_{stem}.png'}")
    print(f"[compare] {outputs_dir / f'compare_file_{stem}.png'}  "
          f"source={source.size} cropped={out.size}")


@pytest.mark.parametrize("path", IMAGE_FILES, ids=lambda p: p.name)
def test_crop_local_file_meta(client, path):
    with path.open("rb") as fh:
        r = client.post("/v1/crop",
                        files={"file": (path.name, fh, "application/octet-stream")},
                        data={"meta": "true"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content_type"] == "image/png"
    assert body["width"] > 0 and body["height"] > 0
    decoded = Image.open(io.BytesIO(base64.b64decode(body["image_base64"])))
    assert decoded.size == (body["width"], body["height"])


@pytest.mark.parametrize("path", IMAGE_FILES, ids=lambda p: p.name)
def test_local_file_matches_direct_function(client, path):
    from autocut_2.image import crop_poster

    with path.open("rb") as fh:
        r = client.post("/v1/crop", files={"file": (path.name, fh, "application/octet-stream")})

    expected = crop_poster(Image.open(path).convert("RGB"))
    assert Image.open(io.BytesIO(r.content)).size == expected.size


@pytest.mark.skipif(not REAL_URLS, reason="tests/url.txt tidak berisi URL")
@pytest.mark.parametrize("url", REAL_URLS, ids=lambda u: f"url{u.find('fbcdn')}")
def test_crop_real_url(client, url, outputs_dir):
    r = client.post("/v1/crop", data={"url": url})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    out = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert out.width > 100 and out.height > 100

    index = REAL_URLS.index(url) + 1
    (outputs_dir / f"url_{index}.png").write_bytes(r.content)
    print(f"\n[output] {outputs_dir / f'url_{index}.png'}  size={out.size}")


@pytest.mark.skipif(not REAL_URLS, reason="tests/url.txt tidak berisi URL")
@pytest.mark.parametrize("url", REAL_URLS, ids=lambda u: f"url{u.find('fbcdn')}")
def test_crop_real_url_meta(client, url):
    r = client.post("/v1/crop", data={"url": url, "meta": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content_type"] == "image/png"
    assert body["width"] > 0 and body["height"] > 0


@pytest.mark.skipif(not REAL_URLS, reason="tests/url.txt tidak berisi URL")
def test_real_url_jpeg_output(client):
    r = client.post("/v1/crop", data={"url": REAL_URLS[0], "format": "jpeg"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(r.content)).format == "JPEG"


def test_missing_source_is_422(client):
    r = client.post("/v1/crop", data={})
    assert r.status_code == 422


def test_invalid_options_rejected(client, path=IMAGE_FILES[0]):
    with path.open("rb") as fh:
        r = client.post("/v1/crop", files={"file": (path.name, fh)},
                        data={"tol": "999", "min_ratio": "0"})
    assert r.status_code == 422


def test_non_image_upload_is_415(client):
    r = client.post("/v1/crop", files={"file": ("x.png", io.BytesIO(b"not an image"), "image/png")})
    assert r.status_code == 415


def test_unreachable_url_is_502(client):
    r = client.post("/v1/crop", data={"url": "http://127.0.0.1:9/missing.png"})
    assert r.status_code == 502


def test_poster_not_found_is_422(client, path=IMAGE_FILES[0]):
    with path.open("rb") as fh:
        r = client.post("/v1/crop", files={"file": (path.name, fh)}, data={"tol": "255"})
    assert r.status_code == 422


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
