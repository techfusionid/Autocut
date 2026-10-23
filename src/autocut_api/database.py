"""SQLite schema and connection helpers for the single-host deployment."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
DATABASE_FILENAME = "jobs.sqlite3"

_CREATE_JOBS_TABLE = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'processing', 'succeeded', 'failed', 'needs_review'
    )),
    mode TEXT NOT NULL CHECK (mode IN ('manual', 'auto')),
    crop_left INTEGER NOT NULL DEFAULT 0 CHECK (crop_left >= 0),
    crop_top INTEGER NOT NULL DEFAULT 0 CHECK (crop_top >= 0),
    crop_right INTEGER NOT NULL DEFAULT 0 CHECK (crop_right >= 0),
    crop_bottom INTEGER NOT NULL DEFAULT 0 CHECK (crop_bottom >= 0),
    source_post_url TEXT,
    original_path TEXT NOT NULL,
    result_path TEXT,
    request_fingerprint TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    input_width INTEGER,
    input_height INTEGER,
    output_width INTEGER,
    output_height INTEGER,
    crop_box_left INTEGER,
    crop_box_top INTEGER,
    crop_box_right INTEGER,
    crop_box_bottom INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    worker_id TEXT,
    worker_lease_expires_at TEXT,
    worker_heartbeat_at TEXT,
    error_code TEXT,
    error_message TEXT
)
"""


def database_path(data_dir: Path) -> Path:
    """Return the fixed SQLite database path beneath the configured data dir."""
    return data_dir.expanduser().resolve() / DATABASE_FILENAME


def connect_database(path: Path) -> sqlite3.Connection:
    """Open an independent connection configured for local API/worker use."""
    connection = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize_database(data_dir: Path) -> Path:
    """Create the persistent directory and schema without resetting existing data."""
    root = data_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / DATABASE_FILENAME
    connection = connect_database(path)
    try:
        # WAL supports the planned local API and worker processes. This database
        # must stay on a local filesystem, not a shared network filesystem.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            )
            """
        )
        version_row = connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
        jobs_table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone() is not None

        if version_row is None:
            if jobs_table_exists:
                raise RuntimeError(
                    "Database has a jobs table but no schema version; refusing to alter it."
                )
            connection.execute(_CREATE_JOBS_TABLE)
            connection.execute(
                "INSERT INTO schema_version (singleton, version) VALUES (1, ?)",
                (SCHEMA_VERSION,),
            )
        elif version_row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported database schema version {version_row['version']}; "
                f"this application supports version {SCHEMA_VERSION}."
            )
        elif not jobs_table_exists:
            raise RuntimeError("Database schema version exists but the jobs table is missing.")

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_queued_order ON jobs(status, created_at, id)"
        )

        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return path
