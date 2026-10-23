"""Reusable bearer-token authentication dependency."""

import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.api_token is None:
        return
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or settings is None
        or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"), settings.api_token.encode("utf-8")
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
