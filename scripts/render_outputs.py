"""Render hasil crop untuk semua aset (file lokal + URL) ke folder outputs/."""
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
OUT = ROOT / "outputs"

sys.path.insert(0, str(ROOT / "src"))
from autocut_2.api import app  # noqa: E402


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = sorted(p for p in TESTS.iterdir()
                   if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    urls = re.findall(r'=\s*"(https://[^"]+)"', (TESTS / "url.txt").read_text())

    with TestClient(app) as client:
        for path in files:
            with path.open("rb") as fh:
                r = client.post("/v1/crop", files={"file": (path.name, fh)})
            stem = path.stem.replace(" ", "_").replace("(", "").replace(")", "")
            target = OUT / f"file_{stem}.png"
            target.write_bytes(r.content)
            print(f"{path.name:20s} -> {target.name:30s} {r.status_code} {len(r.content):>9d} B")

        for i, url in enumerate(urls, 1):
            r = client.post("/v1/crop", data={"url": url})
            target = OUT / f"url_{i}.png"
            target.write_bytes(r.content)
            print(f"url_{i:<18d} -> {target.name:30s} {r.status_code} {len(r.content):>9d} B")


if __name__ == "__main__":
    main()
