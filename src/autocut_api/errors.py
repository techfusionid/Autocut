"""Shared API error response handling."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_404_NOT_FOUND


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if isinstance(exc.detail, dict) and isinstance(exc.detail.get("code"), str):
            code = exc.detail["code"]
            message = exc.detail.get("message", "Request failed.")
        elif exc.status_code == HTTP_401_UNAUTHORIZED:
            code = "authentication_failed"
            message = "Valid bearer credentials are required."
        elif exc.status_code == HTTP_404_NOT_FOUND:
            code = "not_found"
            message = str(exc.detail) if isinstance(exc.detail, str) else "Resource not found."
        else:
            code = "http_error"
            message = str(exc.detail) if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                }
            },
        )


def api_error(status_code: int, code: str, message: str) -> StarletteHTTPException:
    """Create an HTTP exception rendered in the shared error structure."""
    return StarletteHTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
