"""API authentication dependency."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """Verify the Bearer token against the configured API token.

    Returns the token string if valid.
    Raises 401 if missing or invalid.
    """
    settings = get_settings()
    expected = settings.knowledge_api_token

    # If no token is configured, skip auth (dev mode)
    if not expected:
        return ""

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials
