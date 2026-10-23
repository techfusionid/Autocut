from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from autocut_api.app import create_app
from autocut_api.config import Settings
from autocut_api.errors import install_error_handlers
from autocut_api.security import require_bearer_token


def _settings(data_dir: Path, token: str = "test-secret") -> Settings:
    return Settings(api_token=token, data_dir=data_dir)


def _app_with_protected_test_route(data_dir: Path) -> FastAPI:
    app = create_app(_settings(data_dir))

    @app.get("/_test/protected", dependencies=[Depends(require_bearer_token)])
    async def protected_test_route() -> dict[str, str]:
        return {"access": "granted"}

    return app


def test_health_is_public_and_reports_only_api_readiness(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "autocut-api"}


@pytest.mark.parametrize("token", [None, "", "   ", "\t\n"])
def test_missing_or_blank_token_disables_auth_for_local_testing(monkeypatch, token) -> None:
    monkeypatch.delenv("API_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("API_TOKEN", token)

    settings = Settings.from_env()
    assert settings.api_token is None
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_environment_defaults_match_specification() -> None:
    settings = Settings.from_env({})

    assert settings.api_token is None
    assert settings.data_dir.as_posix() == "data"
    assert settings.max_upload_bytes == 15 * 1024 * 1024
    assert settings.max_image_pixels == 25_000_000
    assert settings.job_timeout_seconds == 60
    assert settings.max_job_attempts == 3


def test_environment_settings_can_be_overridden() -> None:
    settings = Settings.from_env(
        {
            "API_TOKEN": "test-secret",
            "DATA_DIR": "/tmp/autocut-data",
            "MAX_UPLOAD_BYTES": "1234",
            "MAX_IMAGE_PIXELS": "5678",
            "JOB_TIMEOUT_SECONDS": "9",
            "MAX_JOB_ATTEMPTS": "2",
        }
    )

    assert settings.data_dir.as_posix() == "/tmp/autocut-data"
    assert settings.max_upload_bytes == 1234
    assert settings.max_image_pixels == 5678
    assert settings.job_timeout_seconds == 9
    assert settings.max_job_attempts == 2


def test_valid_bearer_token_is_accepted(tmp_path: Path) -> None:
    with TestClient(_app_with_protected_test_route(tmp_path)) as client:
        response = client.get("/_test/protected", headers={"Authorization": "Bearer test-secret"})

    assert response.status_code == 200
    assert response.json() == {"access": "granted"}


def test_protected_test_route_allows_requests_when_token_is_not_configured(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path))

    @app.get("/_test/protected", dependencies=[Depends(require_bearer_token)])
    async def protected_test_route() -> dict[str, str]:
        return {"access": "granted"}

    with TestClient(app) as client:
        response = client.get("/_test/protected")

    assert response.status_code == 200
    assert response.json() == {"access": "granted"}


def test_job_api_is_callable_without_credentials_when_token_is_not_configured(tmp_path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (2, 2), "navy")
    payload = BytesIO()
    image.save(payload, format="PNG")
    app = create_app(Settings(data_dir=tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/jobs",
            files={"image": ("small.png", payload.getvalue(), "image/png")},
            data={"mode": "manual"},
        )
        status = client.get(created.json()["status_url"]) if created.status_code == 202 else None

    assert created.status_code == 202
    assert status is not None and status.status_code == 200
    assert status.json()["status"] == "queued"


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-secret", "Basic test-secret"],
)
def test_missing_incorrect_or_unsupported_authentication_is_rejected(tmp_path: Path, authorization) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(_app_with_protected_test_route(tmp_path)) as client:
        response = client.get("/_test/protected", headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "authentication_failed",
            "message": "Valid bearer credentials are required.",
        }
    }
    assert response.headers["www-authenticate"] == "Bearer"


def test_http_errors_use_specification_error_shape() -> None:
    app = FastAPI()
    install_error_handlers(app)

    with TestClient(app) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Not Found"}
    }


def test_request_validation_errors_use_specification_error_shape() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/_test/validation")
    async def validation_test_route(count: int) -> dict[str, int]:
        return {"count": count}

    with TestClient(app) as client:
        response = client.get("/_test/validation", params={"count": "not-an-integer"})

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "Request validation failed.",
        }
    }
