"""FastAPI application factory and health endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from autocut_api.config import Settings
from autocut_api.database import initialize_database
from autocut_api.errors import install_error_handlers
from autocut_api.routes import router as jobs_router
from autocut_api.uploads import RequestBodyLimitMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings if settings is not None else Settings.from_env()
        initialize_database(application.state.settings.data_dir)
        yield

    application = FastAPI(title="Autocut API", lifespan=lifespan)
    install_error_handlers(application)
    application.add_middleware(RequestBodyLimitMiddleware)
    application.include_router(jobs_router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "autocut-api"}

    return application
