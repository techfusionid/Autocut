from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import autocut_api.jobs as jobs
from autocut_api.database import connect_database, database_path, initialize_database
from autocut_api.jobs import (
    IdempotencyConflict,
    create_queued_job,
    fingerprint_request,
    get_job,
)

IMAGE_BYTES = b"original upload bytes\x00\xff\x10"


def test_initialization_is_repeatable_and_existing_data_survives_reopen(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path)
    initialize_database(tmp_path)
    connection = connect_database(db_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, status, mode, original_path, request_fingerprint, created_at, updated_at) "
            "VALUES ('existing-id', 'queued', 'manual', '/private/original', 'fingerprint', 'now', 'now')"
        )
    finally:
        connection.close()

    assert initialize_database(tmp_path) == db_path
    reopened = connect_database(database_path(tmp_path))
    try:
        version = reopened.execute("SELECT version FROM schema_version").fetchone()["version"]
        row = reopened.execute("SELECT id, status FROM jobs WHERE id = 'existing-id'").fetchone()
    finally:
        reopened.close()

    assert version == 1
    assert tuple(row) == ("existing-id", "queued")


def test_job_creation_stores_exact_bytes_and_lookup_returns_metadata(tmp_path: Path) -> None:
    initialize_database(tmp_path)
    job = create_queued_job(
        tmp_path,
        IMAGE_BYTES,
        mode="manual",
        crop_left=7,
        source_post_url="https://example.test/post/1",
    )

    retrieved = get_job(tmp_path, job["id"])

    assert retrieved is not None
    assert retrieved["id"] == job["id"]
    assert retrieved["status"] == "queued"
    assert retrieved["mode"] == "manual"
    assert retrieved["crop_left"] == 7
    assert retrieved["source_post_url"] == "https://example.test/post/1"
    assert retrieved["result_path"] is None
    assert retrieved["input_width"] is None
    assert Path(retrieved["original_path"]).read_bytes() == IMAGE_BYTES
    assert get_job(tmp_path, "missing") is None


def test_fingerprint_is_deterministic_and_includes_normalized_parameters() -> None:
    defaulted = fingerprint_request(IMAGE_BYTES, mode="manual")
    explicit_defaults = fingerprint_request(
        IMAGE_BYTES,
        mode="manual",
        crop_left=0,
        crop_top=0,
        crop_right=0,
        crop_bottom=0,
    )

    assert defaulted == explicit_defaults
    assert defaulted != fingerprint_request(IMAGE_BYTES + b"x", mode="manual")
    assert defaulted != fingerprint_request(IMAGE_BYTES, mode="manual", crop_left=1)
    assert defaulted != fingerprint_request(
        IMAGE_BYTES, mode="manual", source_post_url="https://example.test/post/1"
    )


def test_idempotency_replays_same_job_and_rejects_different_fingerprint(tmp_path: Path) -> None:
    initialize_database(tmp_path)
    first = create_queued_job(tmp_path, IMAGE_BYTES, mode="manual", idempotency_key="retry-1")
    replay = create_queued_job(tmp_path, IMAGE_BYTES, mode="manual", idempotency_key="retry-1")

    assert replay["id"] == first["id"]
    assert replay["original_path"] == first["original_path"]
    assert len(list((tmp_path / "originals").glob("*.original"))) == 1
    with pytest.raises(IdempotencyConflict, match="different request"):
        create_queued_job(
            tmp_path,
            IMAGE_BYTES,
            mode="manual",
            crop_right=1,
            idempotency_key="retry-1",
        )
    assert get_job(tmp_path, first["id"])["request_fingerprint"] == first["request_fingerprint"]


def test_concurrent_same_key_submissions_keep_one_job_and_original(tmp_path: Path) -> None:
    initialize_database(tmp_path)

    def submit() -> dict:
        return create_queued_job(
            tmp_path,
            IMAGE_BYTES,
            mode="manual",
            idempotency_key="concurrent-retry",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs_created = list(pool.map(lambda _: submit(), range(8)))

    assert len({job["id"] for job in jobs_created}) == 1
    winning_job = get_job(tmp_path, jobs_created[0]["id"])
    assert Path(winning_job["original_path"]).read_bytes() == IMAGE_BYTES
    connection = connect_database(database_path(tmp_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        connection.close()
    assert len(list((tmp_path / "originals").glob("*.original"))) == 1
    assert list((tmp_path / "originals").glob("*.tmp")) == []


def test_storage_write_failure_leaves_no_job_or_partial_file(tmp_path: Path, monkeypatch) -> None:
    initialize_database(tmp_path)

    def fail_write(*args, **kwargs):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(jobs, "_write_staged_original", fail_write)
    with pytest.raises(OSError, match="simulated storage failure"):
        create_queued_job(tmp_path, IMAGE_BYTES, mode="manual")

    connection = connect_database(database_path(tmp_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        connection.close()
    assert list((tmp_path / "originals").iterdir()) == []


def test_database_insert_failure_removes_finalized_file_and_job(tmp_path: Path, monkeypatch) -> None:
    initialize_database(tmp_path)

    def fail_insert(connection, values):
        raise sqlite3.IntegrityError("simulated database insertion failure")

    monkeypatch.setattr(jobs, "_insert_queued_job", fail_insert)
    with pytest.raises(sqlite3.IntegrityError, match="simulated database insertion failure"):
        create_queued_job(tmp_path, IMAGE_BYTES, mode="manual")

    connection = connect_database(database_path(tmp_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        connection.close()
    assert list((tmp_path / "originals").iterdir()) == []


def test_database_open_failure_cleans_staged_original(tmp_path: Path, monkeypatch) -> None:
    initialize_database(tmp_path)

    def fail_connect(path):
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(jobs, "connect_database", fail_connect)
    with pytest.raises(sqlite3.OperationalError, match="simulated database failure"):
        create_queued_job(tmp_path, IMAGE_BYTES, mode="manual")

    assert list((tmp_path / "originals").iterdir()) == []
