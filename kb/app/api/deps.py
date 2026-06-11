"""API authentication dependency."""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

logger = logging.getLogger(__name__)

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

    # SECURITY: If no token is configured, reject all requests.
    # Never skip authentication — an empty token is a misconfiguration, not dev mode.
    if not expected:
        logger.error("API token is not configured. Rejecting request.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token not configured on server",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # SECURITY: Use timing-safe comparison to prevent timing attacks
    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials
