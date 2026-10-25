"""One-prompt local image cutter; it does not start an API or worker."""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from autocut_api.app import ImageError, Settings, crop_image_bytes


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    downloads = root / "downloads"
    settings = Settings.from_env()
    print("Autocut — potong otomatis")
    print("Tempel atau seret path gambar, lalu Enter. Ketik q untuk keluar.")
    while True:
        try:
            entered = input("Gambar: ").strip()
        except EOFError:
            print()
            return 0
        if entered.lower() == "q":
            return 0
        try:
            parts = shlex.split(entered)
            if len(parts) != 1:
                raise ValueError("Masukkan satu path gambar.")
            source = Path(parts[0]).expanduser()
            if source.stat().st_size > settings.max_upload_bytes:
                raise ValueError("Ukuran gambar melebihi batas upload.")
            payload = source.read_bytes()
            result = crop_image_bytes(
                payload,
                mode="auto",
                max_image_pixels=settings.max_image_pixels,
            )
            destination = downloads / f"{source.stem}-cropped.png"
            _save_atomically(destination, result)
            print(f"Hasil tersimpan: {destination}\n")
        except ImageError as exc:
            print(f"Gambar tidak dipotong: {exc.message}\n")
        except (OSError, ValueError) as exc:
            print(f"Tidak dapat memproses file: {exc}\n")


def _save_atomically(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=f".{destination.stem}.",
            suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
