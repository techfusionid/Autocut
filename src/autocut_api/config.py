"""Environment-based application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class SettingsError(ValueError):
    """Raised when required application configuration is invalid."""


@dataclass(frozen=True)
class WorkerSettings:
    """The worker's non-secret settings; it does not need the API bearer token."""

    data_dir: Path = Path("data")
    max_image_pixels: int = 25_000_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WorkerSettings:
        values = os.environ if environ is None else environ
        return cls(
            data_dir=Path(values.get("DATA_DIR", "data")),
            max_image_pixels=_positive_int(values, "MAX_IMAGE_PIXELS", 25_000_000),
        )

    def __post_init__(self) -> None:
        if self.max_image_pixels <= 0:
            raise SettingsError("MAX_IMAGE_PIXELS must be a positive integer.")


@dataclass(frozen=True, repr=False)
class Settings:
    api_token: str | None = None
    data_dir: Path = Path("data")
    max_upload_bytes: int = 15 * 1024 * 1024
    max_image_pixels: int = 25_000_000
    job_timeout_seconds: int = 60
    max_job_attempts: int = 3

    def __post_init__(self) -> None:
        if self.api_token is not None and not self.api_token.strip():
            object.__setattr__(self, "api_token", None)

        for name in (
            "max_upload_bytes",
            "max_image_pixels",
            "job_timeout_seconds",
            "max_job_attempts",
        ):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name.upper()} must be a positive integer.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        api_token = values.get("API_TOKEN")
        return cls(
            api_token=api_token if api_token and api_token.strip() else None,
            data_dir=Path(values.get("DATA_DIR", "data")),
            max_upload_bytes=_positive_int(values, "MAX_UPLOAD_BYTES", 15 * 1024 * 1024),
            max_image_pixels=_positive_int(values, "MAX_IMAGE_PIXELS", 25_000_000),
            job_timeout_seconds=_positive_int(values, "JOB_TIMEOUT_SECONDS", 60),
            max_job_attempts=_positive_int(values, "MAX_JOB_ATTEMPTS", 3),
        )


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise SettingsError(f"{name} must be a positive integer.")
    return value
