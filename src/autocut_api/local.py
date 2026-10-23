"""Small local supervisor for running the API and worker with one command."""

from __future__ import annotations

import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from autocut_api.jobs import get_job, list_succeeded_jobs


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    source_dir = project_root / "src"
    environment = os.environ.copy()
    environment.setdefault("DATA_DIR", str(project_root / "data"))
    data_dir = Path(environment["DATA_DIR"]).expanduser().resolve()
    current_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_dir)
        if not current_pythonpath
        else f"{source_dir}{os.pathsep}{current_pythonpath}"
    )

    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "autocut_api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=project_root,
        env=environment,
    )
    worker: subprocess.Popen[bytes] | None = None

    try:
        startup_deadline = time.monotonic() + 30
        while time.monotonic() < startup_deadline:
            if api.poll() is not None:
                return api.returncode or 1
            try:
                with urlopen("http://127.0.0.1:8000/health", timeout=0.5):
                    break
            except (URLError, TimeoutError):
                time.sleep(0.25)
        else:
            print("Autocut API did not become ready within 30 seconds.", file=sys.stderr)
            return 1

        worker = subprocess.Popen(
            [sys.executable, "-m", "autocut_api.worker"],
            cwd=project_root,
            env=environment,
        )
        print("\nAutocut berjalan di http://127.0.0.1:8000", flush=True)
        print("Pilih gambar dari menu ini untuk mengunggah dan memprosesnya.\n", flush=True)
        processes = (api, worker)
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    return exit_code
            print("Menu Autocut")
            print("  1. Upload gambar dan potong otomatis")
            print("  2. Simpan lagi hasil yang sudah selesai")
            print("  q. Keluar dan hentikan API + worker")
            try:
                choice = input("Pilihan: ").strip().lower()
            except EOFError:
                return 0
            if choice == "q":
                return 0
            if choice == "1":
                _upload_from_terminal(data_dir, project_root / "downloads")
            elif choice == "2":
                _choose_and_save_result(data_dir, project_root / "downloads")
            else:
                print("Pilihan tidak dikenal. Masukkan 1, 2, atau q.\n", flush=True)
    except KeyboardInterrupt:
        print("\nStopping Autocut local services...", flush=True)
        return 0
    finally:
        processes = (api,) if worker is None else (api, worker)
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _upload_from_terminal(data_dir: Path, downloads_dir: Path) -> None:
    try:
        entered_path = input(
            "Seret file gambar ke terminal atau tempel path file, lalu tekan Enter: "
        ).strip()
    except EOFError:
        print("\nKembali ke menu.\n", flush=True)
        return
    if not entered_path:
        print("Path gambar kosong. Kembali ke menu.\n", flush=True)
        return

    try:
        parsed_path = shlex.split(entered_path)
        if len(parsed_path) != 1:
            raise ValueError("Enter exactly one image path.")
        image_path = Path(parsed_path[0]).expanduser()
        max_upload_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", 15 * 1024 * 1024))
        if image_path.stat().st_size > max_upload_bytes:
            print("Ukuran gambar melebihi batas upload yang dikonfigurasi.\n", flush=True)
            return
        image_bytes = image_path.read_bytes()
    except (OSError, ValueError):
        print("File tidak dapat dibaca. Tempel path ke satu file JPEG atau PNG.\n", flush=True)
        return
    if not image_bytes:
        print("File gambar kosong.\n", flush=True)
        return

    try:
        job = _submit_auto_upload(image_path, image_bytes)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        detail = _http_error_message(exc)
        print(f"Upload gagal: {detail}\n", flush=True)
        return

    job_id = job.get("job_id")
    status_url = job.get("status_url")
    if not isinstance(job_id, str) or not isinstance(status_url, str):
        print("API mengirim respons yang tidak dikenali.\n", flush=True)
        return
    print(f"Upload diterima. Job: {job_id}", flush=True)
    print("Menunggu worker memproses gambar...", flush=True)
    try:
        status = _wait_for_job(status_url)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print(
            f"Status belum dapat diperiksa: {_http_error_message(exc)}. "
            f"Job {job_id} tetap tersimpan; pilih menu 2 nanti.\n",
            flush=True,
        )
        return

    if status.get("status") == "succeeded":
        try:
            destination = _copy_completed_result(data_dir, job_id, downloads_dir)
        except (OSError, ValueError):
            print("Pemotongan selesai, tetapi PNG gagal disimpan ke folder downloads.\n", flush=True)
            return
        print(f"Selesai. Hasil tersimpan di:\n{destination}\n", flush=True)
    elif status.get("status") == "needs_review":
        print("Batas bingkai belum cukup jelas. Tidak ada hasil otomatis yang dibuat.\n", flush=True)
    else:
        error = status.get("error") or {}
        message = error.get("message", "Worker gagal memproses gambar.")
        print(f"Pemrosesan berakhir dengan status {status.get('status')}: {message}\n", flush=True)


def _submit_auto_upload(image_path: Path, image_bytes: bytes) -> dict[str, object]:
    boundary = f"autocut-{uuid.uuid4().hex}"
    filename = image_path.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(),
            image_bytes,
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\nauto"
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    request = Request(
        "http://127.0.0.1:8000/jobs",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return _read_json_response(request)


def _wait_for_job(status_url: str, timeout_seconds: int = 120) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    absolute_url = urljoin("http://127.0.0.1:8000", status_url)
    while time.monotonic() < deadline:
        status = _read_json_response(Request(absolute_url, method="GET"))
        if status.get("status") in {"succeeded", "failed", "needs_review"}:
            return status
        time.sleep(1)
    raise TimeoutError("Pemrosesan belum selesai dalam 2 menit.")


def _read_json_response(request: Request) -> dict[str, object]:
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("API returned an invalid response.")
    return payload


def _http_error_message(error: Exception) -> str:
    if isinstance(error, HTTPError):
        try:
            payload = json.loads(error.read())
            return payload.get("error", {}).get("message", f"HTTP {error.code}")
        except (json.JSONDecodeError, AttributeError, TypeError):
            return f"HTTP {error.code}"
    if isinstance(error, TimeoutError):
        return str(error)
    if isinstance(error, URLError):
        return "API tidak dapat dihubungi"
    return str(error)


def _choose_and_save_result(data_dir: Path, downloads_dir: Path) -> None:
    jobs = list_succeeded_jobs(data_dir)
    if not jobs:
        print("Belum ada hasil yang selesai. Setelah worker memproses upload, coba lagi.\n", flush=True)
        return

    print("\nHasil selesai:")
    for number, job in enumerate(jobs, start=1):
        dimensions = "ukuran tidak tersedia"
        if job["output_width"] is not None and job["output_height"] is not None:
            dimensions = f"{job['output_width']}×{job['output_height']} px"
        print(f"  {number}. {job['id']} — {job['mode']} — {dimensions}")
    try:
        selection = input("Nomor hasil (Enter untuk kembali): ").strip()
    except EOFError:
        print("", flush=True)
        return
    if not selection:
        print("Kembali ke menu.\n", flush=True)
        return
    if not selection.isdecimal() or not 1 <= int(selection) <= len(jobs):
        print("Nomor hasil tidak valid.\n", flush=True)
        return

    job = jobs[int(selection) - 1]
    try:
        destination = _copy_completed_result(data_dir, job["id"], downloads_dir)
    except (OSError, ValueError):
        print("Hasil tidak dapat disimpan. Periksa penyimpanan lokal dan coba lagi.\n", flush=True)
        return
    print(f"PNG tersimpan: {destination}\n", flush=True)


def _copy_completed_result(data_dir: Path, job_id: str, downloads_dir: Path) -> Path:
    """Copy a succeeded PNG to the local downloads folder without partial output."""
    if not job_id or Path(job_id).name != job_id or job_id in {".", ".."}:
        raise ValueError("Invalid job identifier.")
    job = get_job(data_dir, job_id)
    if job is None or job["status"] != "succeeded" or not job["result_path"]:
        raise ValueError("Completed result is unavailable.")

    source = Path(job["result_path"])
    downloads_dir.mkdir(parents=True, exist_ok=True)
    destination = downloads_dir / f"autocut-{job_id}.png"
    temporary_path: Path | None = None
    try:
        with source.open("rb") as input_file, tempfile.NamedTemporaryFile(
            mode="w+b", prefix=f".{destination.stem}.", suffix=".tmp",
            dir=downloads_dir, delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        return destination
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
