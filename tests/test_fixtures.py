"""Crop the real fixtures in tests/ over the real /crop route and write results to tests/.

Covers every local image next to this file and every URL in tests/url.txt. Each case
writes ``<name>-cropped.png`` plus ``<name>-original.*`` (the source that was cropped),
so the result can be compared against the input by eye.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat
from starlette.testclient import TestClient

from autocut_api.app import create_app
from autocut_api.fetch import FetchError, fetch_image_url

TESTS_DIR = Path(__file__).parent
URL_FILE = TESTS_DIR / "url.txt"
SOURCE_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}
# Only inputs, never the -cropped/-original outputs this test writes.
IMAGE_FILES = sorted(
    path
    for path in TESTS_DIR.iterdir()
    if path.suffix.lower() in SOURCE_EXTENSIONS
    and not path.stem.endswith(("-cropped", "-original"))
)
REAL_URLS = re.findall(r'=\s*"(https://[^"]+)"', URL_FILE.read_text()) if URL_FILE.exists() else []

# TestClient in this venv wraps httpx2, which encodes ``data=`` as urlencoded form
# instead of multipart; plain fields therefore go through ``files=`` with a None filename.
def multipart(**fields: str) -> dict[str, tuple[None, str]]:
    return {name: (None, value) for name, value in fields.items()}


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


def crop_via_route(
    client: TestClient, source: dict[str, tuple[str, bytes]] | None, fields: dict[str, tuple[None, str]]
) -> tuple[bytes, str]:
    """Post once with auto mode, falling back to manual when detection is unsure."""
    files = {**(source or {}), **fields, **multipart(mode="auto")}
    response = client.post("/crop", files=files)
    used = "auto"
    if response.status_code == 422 and response.json()["error"]["code"] == "frame_uncertain":
        files = {**(source or {}), **fields, **multipart(mode="manual")}
        response = client.post("/crop", files=files)
        used = "manual"
    assert response.status_code == 200, response.text
    return response.content, used


def _assert_real_crop(source: bytes, result: bytes, label: str) -> str:
    with Image.open(source) as original:
        expected = original.size
    with Image.open(result) as cropped:
        assert cropped.format == "PNG"
        assert cropped.size[0] <= expected[0] and cropped.size[1] <= expected[1]
        if cropped.size == expected:
            return f"{label}: NOTHING REMOVED ({cropped.size})"
        return f"{label}: {expected} -> {cropped.size}"


def _difference(source: bytes, result: bytes) -> float:
    """Mean per-channel difference between the source top-left area and the crop."""
    with Image.open(source) as original, Image.open(result) as cropped:
        overlap = original.convert("RGB").crop((0, 0, cropped.width, cropped.height))
        return sum(ImageStat.Stat(ImageChops.difference(overlap, cropped.convert("RGB"))).mean) / 3


@pytest.mark.parametrize("path", IMAGE_FILES, ids=lambda path: path.name)
def test_crop_local_file(client: TestClient, path: Path) -> None:
    payload = path.read_bytes()
    result, mode = crop_via_route(client, {"image": (path.name, payload)}, {})

    (TESTS_DIR / f"{path.stem}-original{path.suffix}").write_bytes(payload)
    destination = TESTS_DIR / f"{path.stem}-cropped.png"
    destination.write_bytes(result)
    summary = _assert_real_crop(payload, result, f"{path.name} [{mode}]")
    print(f"\n[crop] {destination.name} {summary} diff={_difference(payload, result):.1f}")


@pytest.mark.skipif(not REAL_URLS, reason="tests/url.txt has no URL")
@pytest.mark.parametrize("url", REAL_URLS, ids=lambda url: url.split("/")[-1].split("_")[0])
def test_crop_real_url(client: TestClient, url: str) -> None:
    index = REAL_URLS.index(url) + 1
    try:
        source = asyncio.run(fetch_image_url(url, max_bytes=15 * 1024 * 1024, timeout=30))
    except FetchError as exc:
        pytest.skip(f"CDN URL expired or unreachable: {exc.code}")

    result, mode = crop_via_route(client, None, multipart(image_url=url))
    suffix = f".{Image.open(__import__('io').BytesIO(source)).format.lower()}"
    (TESTS_DIR / f"url_{index}-original{suffix}").write_bytes(source)
    destination = TESTS_DIR / f"url_{index}-cropped.png"
    destination.write_bytes(result)
    summary = _assert_real_crop(source, result, f"url {index} [{mode}]")
    print(f"\n[crop] {destination.name} {summary} diff={_difference(source, result):.1f}")
