"""Queued job persistence and atomic storage of original upload bytes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autocut_api.database import connect_database, database_path


class IdempotencyConflict(Exception):
    """An idempotency key was previously used with a different request."""


def fingerprint_request(
    image_bytes: bytes,
    *,
    mode: str,
    source_post_url: str | None = None,
    crop_left: int = 0,
    crop_top: int = 0,
    crop_right: int = 0,
    crop_bottom: int = 0,
) -> str:
    """Hash the exact image bytes and normalized request parameters.

    Parameters are mode, source_post_url, and all four integer crop margins.
    Callers should apply API defaults before calling; omitted manual margins are
    represented here as zero. Filenames and caller-supplied MIME types do not
    participate because they do not affect the stored original or processing.
    """
    if mode not in {"manual", "auto"}:
        raise ValueError("mode must be 'manual' or 'auto'.")
    if source_post_url is not None and not isinstance(source_post_url, str):
        raise ValueError("source_post_url must be a string or None.")

    margins = (crop_left, crop_top, crop_right, crop_bottom)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in margins):
        raise ValueError("crop margins must be nonnegative integers.")

    parameters = {
        "crop_bottom": crop_bottom,
        "crop_left": crop_left,
        "crop_right": crop_right,
        "crop_top": crop_top,
        "mode": mode,
        "source_post_url": source_post_url,
    }
    encoded_parameters = json.dumps(
        parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"autocut-request-fingerprint-v1\0")
    digest.update(len(image_bytes).to_bytes(8, "big"))
    digest.update(image_bytes)
    digest.update(encoded_parameters)
    return digest.hexdigest()


def create_queued_job(
    data_dir: Path,
    image_bytes: bytes,
    *,
    mode: str,
    source_post_url: str | None = None,
    crop_left: int = 0,
    crop_top: int = 0,
    crop_right: int = 0,
    crop_bottom: int = 0,
    idempotency_key: str | None = None,
    input_width: int | None = None,
    input_height: int | None = None,
) -> dict[str, Any]:
    """Store original bytes and insert a queued job in one consistency flow.

    `initialize_database` must have completed before this function is called.
    The returned mapping is internal and contains filesystem paths; callers must
    never serialize it directly as an API response.
    """
    root = data_dir.expanduser().resolve()
    original_dir = root / "originals"
    original_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = fingerprint_request(
        image_bytes,
        mode=mode,
        source_post_url=source_post_url,
        crop_left=crop_left,
        crop_top=crop_top,
        crop_right=crop_right,
        crop_bottom=crop_bottom,
    )
    job_id = str(uuid.uuid4())
    final_path = original_dir / f"{job_id}.original"
    staged_path = _write_staged_original(original_dir, job_id, image_bytes)
    finalized = False
    connection: sqlite3.Connection | None = None

    try:
        connection = connect_database(database_path(root))
        connection.execute("BEGIN IMMEDIATE")

        if idempotency_key is not None:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if existing["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict(
                        "Idempotency key was already used for a different request."
                    )
                return dict(existing)

        now = _utc_now()
        os.replace(staged_path, final_path)
        staged_path = None
        finalized = True
        _sync_directory(original_dir)

        values = (
            job_id,
            mode,
            crop_left,
            crop_top,
            crop_right,
            crop_bottom,
            source_post_url,
            str(final_path),
            fingerprint,
            idempotency_key,
            now,
            input_width,
            input_height,
        )
        _insert_queued_job(connection, values)
        connection.commit()
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeError("Queued job was not available after commit.")
        return dict(row)
    except BaseException:
        if connection is not None:
            connection.rollback()
        if finalized and not _job_record_exists(connection, job_id):
            _remove_if_present(final_path)
        raise
    finally:
        if connection is not None:
            connection.close()
        if staged_path is not None:
            _remove_if_present(staged_path)


def get_job(data_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Retrieve one job's internal metadata, or None when it does not exist."""
    connection = connect_database(database_path(data_dir.expanduser().resolve()))
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def list_succeeded_jobs(data_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent completed jobs for the local terminal menu."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer.")
    connection = connect_database(database_path(data_dir.expanduser().resolve()))
    try:
        rows = connection.execute(
            """SELECT id, mode, finished_at, output_width, output_height, result_path
               FROM jobs WHERE status = 'succeeded'
               ORDER BY finished_at DESC, created_at DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def claim_next_job(data_dir: Path, worker_id: str) -> dict[str, Any] | None:
    """Atomically move the oldest queued job to processing and return it."""
    connection = connect_database(database_path(data_dir.expanduser().resolve()))
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return None

        now = _utc_now()
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = 'processing', started_at = ?, updated_at = ?,
                attempt_count = attempt_count + 1, worker_id = ?,
                worker_lease_expires_at = NULL, worker_heartbeat_at = NULL
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, worker_id, row["id"]),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        connection.commit()
        return dict(claimed) if claimed is not None else None
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_job_succeeded(
    data_dir: Path,
    job_id: str,
    worker_id: str,
    *,
    result_path: Path,
    input_width: int,
    input_height: int,
    output_width: int,
    output_height: int,
    crop_box: tuple[int, int, int, int],
) -> None:
    """Record a completed job after its atomically finalized result exists."""
    if not result_path.is_file():
        raise FileNotFoundError("Final result file is unavailable.")
    now = _utc_now()
    connection = connect_database(database_path(data_dir.expanduser().resolve()))
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE jobs SET status = 'succeeded', result_path = ?,
                input_width = ?, input_height = ?, output_width = ?, output_height = ?,
                crop_box_left = ?, crop_box_top = ?, crop_box_right = ?, crop_box_bottom = ?,
                finished_at = ?, updated_at = ?, error_code = NULL, error_message = NULL
            WHERE id = ? AND status = 'processing' AND worker_id = ?
            """,
            (
                str(result_path), input_width, input_height, output_width, output_height,
                *crop_box, now, now, job_id, worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Job is no longer owned by this worker.")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_job_failed(
    data_dir: Path,
    job_id: str,
    worker_id: str,
    *,
    error_code: str,
    error_message: str,
) -> None:
    """Record a terminal processing failure for the worker that claimed it."""
    now = _utc_now()
    connection = connect_database(database_path(data_dir.expanduser().resolve()))
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE jobs SET status = 'failed', result_path = NULL,
                output_width = NULL, output_height = NULL,
                crop_box_left = NULL, crop_box_top = NULL,
                crop_box_right = NULL, crop_box_bottom = NULL,
                finished_at = ?, updated_at = ?, error_code = ?, error_message = ?
            WHERE id = ? AND status = 'processing' AND worker_id = ?
            """,
            (now, now, error_code, error_message, job_id, worker_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Job is no longer owned by this worker.")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_job_needs_review(
    data_dir: Path,
    job_id: str,
    worker_id: str,
    *,
    error_code: str,
    error_message: str,
) -> None:
    """Finish an uncertain automatic crop without publishing a result file."""
    now = _utc_now()
    connection = connect_database(database_path(data_dir.expanduser().resolve()))
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE jobs SET status = 'needs_review', result_path = NULL,
                output_width = NULL, output_height = NULL,
                crop_box_left = NULL, crop_box_top = NULL,
                crop_box_right = NULL, crop_box_bottom = NULL,
                finished_at = ?, updated_at = ?, error_code = ?, error_message = ?
            WHERE id = ? AND status = 'processing' AND worker_id = ?
            """,
            (now, now, error_code, error_message, job_id, worker_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Job is no longer owned by this worker.")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _insert_queued_job(connection: sqlite3.Connection, values: tuple[Any, ...]) -> None:
    connection.execute(
        """
        INSERT INTO jobs (
            id, status, mode, crop_left, crop_top, crop_right, crop_bottom,
            source_post_url, original_path, request_fingerprint, idempotency_key,
            input_width, input_height, created_at, updated_at
        ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values[:10], values[11], values[12], values[10], values[10]),
    )


def _write_staged_original(original_dir: Path, job_id: str, image_bytes: bytes) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{job_id}.",
            suffix=".tmp",
            dir=original_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(image_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        return temporary_path
    except BaseException:
        if temporary_path is not None:
            _remove_if_present(temporary_path)
        raise


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _job_record_exists(connection: sqlite3.Connection | None, job_id: str) -> bool:
    if connection is None:
        return False
    try:
        return connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
