"""ASGI entry point for Uvicorn."""

from autocut_api.app import create_app

app = create_app()
