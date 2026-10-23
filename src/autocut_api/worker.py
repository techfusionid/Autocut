"""Single-process SQLite worker for deterministic manual image crops."""

from __future__ import annotations

import argparse
import os
import signal
import tempfile
import threading
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from autocut_api.config import WorkerSettings
from autocut_api.database import initialize_database
from autocut_api.detection import FrameDetectionUncertain, detect_frame_box
from autocut_api.jobs import (
    claim_next_job,
    get_job,
    mark_job_failed,
    mark_job_needs_review,
    mark_job_succeeded,
)
from autocut_api.uploads import ImageValidationError, validate_image_bytes

WORKER_HEARTBEAT_FILENAME = "worker.heartbeat"
WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class ProcessingFailure(Exception):
    code: str
    message: str


def process_next_job(
    data_dir: Path,
    max_image_pixels: int,
    *,
    worker_id: str | None = None,
) -> dict[str, str] | None:
    """Claim and process at most one job; return None when the queue is empty."""
    owner = worker_id or f"worker-{uuid.uuid4()}"
    job = claim_next_job(data_dir, owner)
    if job is None:
        return None

    result_path = (data_dir.expanduser().resolve() / "results" / f"{job['id']}.png")
    try:
        input_width, input_height, crop_box, output_width, output_height = _process_image(
            job, max_image_pixels, result_path
        )
        mark_job_succeeded(
            data_dir,
            job["id"],
            owner,
            result_path=result_path,
            input_width=input_width,
            input_height=input_height,
            output_width=output_width,
            output_height=output_height,
            crop_box=crop_box,
        )
        return {"job_id": job["id"], "status": "succeeded"}
    except ProcessingFailure as exc:
        _remove_result_unless_succeeded(data_dir, job["id"], result_path)
        mark_job_failed(
            data_dir, job["id"], owner, error_code=exc.code, error_message=exc.message
        )
        return {"job_id": job["id"], "status": "failed"}
    except FrameDetectionUncertain:
        _remove_result_unless_succeeded(data_dir, job["id"], result_path)
        mark_job_needs_review(
            data_dir,
            job["id"],
            owner,
            error_code="frame_uncertain",
            error_message="A clear rectangular frame could not be identified; manual review is required.",
        )
        return {"job_id": job["id"], "status": "needs_review"}
    except Exception:
        # Keep paths and exception text out of job errors and worker output.
        succeeded = _remove_result_unless_succeeded(data_dir, job["id"], result_path)
        if succeeded:
            return {"job_id": job["id"], "status": "succeeded"}
        mark_job_failed(
            data_dir,
            job["id"],
            owner,
            error_code="processing_failed",
            error_message="Image processing failed.",
        )
        return {"job_id": job["id"], "status": "failed"}


def _process_image(
    job: dict[str, Any], max_image_pixels: int, result_path: Path
) -> tuple[int, int, tuple[int, int, int, int], int, int]:
    if job["mode"] not in {"manual", "auto"}:
        raise ProcessingFailure("invalid_mode", "Stored processing mode is invalid.")

    try:
        original_bytes = Path(job["original_path"]).read_bytes()
    except OSError:
        raise ProcessingFailure("original_missing", "The stored original image is unavailable.") from None

    try:
        validated = validate_image_bytes(original_bytes, max_image_pixels)
    except ImageValidationError as exc:
        raise ProcessingFailure(exc.code, exc.message) from None

    try:
        with Image.open(BytesIO(original_bytes)) as opened:
            opened.load()
            normalized = ImageOps.exif_transpose(opened)
            input_width, input_height = normalized.size
            if (input_width, input_height) != (validated.width, validated.height):
                raise ProcessingFailure("invalid_image", "Image dimensions could not be validated.")
            if job["mode"] == "auto":
                try:
                    crop_box = detect_frame_box(normalized)
                except FrameDetectionUncertain:
                    raise
            else:
                margins = tuple(
                    job[name]
                    for name in ("crop_left", "crop_top", "crop_right", "crop_bottom")
                )
                if any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                    for value in margins
                ):
                    raise ProcessingFailure("invalid_crop", "Stored crop margins are invalid.")
                left, top, right_margin, bottom_margin = margins
                right = input_width - right_margin
                bottom = input_height - bottom_margin
                if left >= right or top >= bottom:
                    raise ProcessingFailure("invalid_crop", "Stored crop margins leave no image area.")
                crop_box = (left, top, right, bottom)
            cropped = normalized.crop(crop_box)
            output_width, output_height = cropped.size
            png_image = _png_compatible(cropped)
            try:
                _save_result_atomically(png_image, result_path)
            except OSError:
                raise ProcessingFailure("result_write_failed", "The processed image could not be saved.") from None
            finally:
                if png_image is not cropped:
                    png_image.close()
                cropped.close()
            return input_width, input_height, crop_box, output_width, output_height
    except ProcessingFailure:
        raise
    except FrameDetectionUncertain:
        raise
    except Exception:
        raise ProcessingFailure("invalid_image", "Image is malformed or could not be decoded.") from None


def _png_compatible(image: Image.Image) -> Image.Image:
    if image.mode == "CMYK":
        return image.convert("RGB")
    if image.mode in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
        return image
    has_alpha = "A" in image.getbands() or (
        image.mode == "P" and "transparency" in image.info
    )
    return image.convert("RGBA" if has_alpha else "RGB")


def _save_result_atomically(image: Image.Image, result_path: Path) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{result_path.stem}.",
            suffix=".tmp",
            dir=result_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            image.save(temporary, format="PNG")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, result_path)
        temporary_path = None
        _sync_directory(result_path.parent)
    except BaseException:
        if temporary_path is not None:
            _unlink(temporary_path)
        raise


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_result_unless_succeeded(data_dir: Path, job_id: str, result_path: Path) -> bool:
    try:
        current = get_job(data_dir, job_id)
    except Exception:
        # If SQLite is unavailable, leave the atomically written file in place;
        # the result endpoint still gates access on succeeded metadata.
        return False
    if current is not None and current["status"] == "succeeded":
        return True
    _unlink(result_path)
    return False


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def run_worker(
    data_dir: Path,
    max_image_pixels: int,
    *,
    once: bool = False,
    poll_interval: float = 1.0,
    stop_event: threading.Event | None = None,
) -> int:
    """Run one job or poll continuously; return the number of jobs processed."""
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    initialize_database(data_dir)
    stop = stop_event or threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat_path = data_dir.expanduser().resolve() / WORKER_HEARTBEAT_FILENAME
    _write_worker_heartbeat(heartbeat_path)
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(heartbeat_path, heartbeat_stop),
        name="autocut-worker-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    processed = 0
    try:
        while not stop.is_set():
            outcome = process_next_job(data_dir, max_image_pixels)
            if outcome is None:
                if once:
                    return processed
                stop.wait(poll_interval)
                continue
            processed += 1
            print(f"Job {outcome['job_id']}: {outcome['status']}.", flush=True)
            if once:
                return processed
        return processed
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=WORKER_HEARTBEAT_INTERVAL_SECONDS + 1)


def _heartbeat_loop(path: Path, stop: threading.Event) -> None:
    while not stop.wait(WORKER_HEARTBEAT_INTERVAL_SECONDS):
        try:
            _write_worker_heartbeat(path)
        except OSError:
            # The container health check will report a stale heartbeat.
            continue


def _write_worker_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Autocut manual image worker.")
    parser.add_argument("--once", action="store_true", help="process at most one queued job and exit")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="idle delay in seconds")
    args = parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    try:
        settings = WorkerSettings.from_env()
    except ValueError as exc:
        parser.error(str(exc))

    stopping = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run_worker(
        settings.data_dir,
        settings.max_image_pixels,
        once=args.once,
        poll_interval=args.poll_interval,
        stop_event=stopping,
    )
    print("Worker stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
