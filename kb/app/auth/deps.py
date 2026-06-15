"""Authentication dependency injection for FastAPI.

Provides:
- get_current_user: JWT-based authentication
- get_current_user_or_service: JWT with service account fallback
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.jwt import decode_access_token
from app.auth.rate_limit import UserRateLimiter
from app.auth.user_store import UserStore
from app.config import get_settings
from app.database import Neo4jDatabase
from app.models import CurrentUser

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

# Global instances — set by main.py lifespan
_user_store: UserStore | None = None
_rate_limiter: UserRateLimiter | None = None


def set_user_store(store: UserStore) -> None:
    """Called by main.py to inject the UserStore singleton."""
    global _user_store
    _user_store = store


def set_rate_limiter(limiter: UserRateLimiter) -> None:
    """Called by main.py to inject the rate limiter singleton."""
    global _rate_limiter
    _rate_limiter = limiter


def get_rate_limiter() -> UserRateLimiter:
    """FastAPI dependency to get the rate limiter."""
    assert _rate_limiter is not None, "Rate limiter not initialized"
    return _rate_limiter


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """Verify JWT access token and return CurrentUser.

    Raises 401 if missing, invalid, or user not found.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Try JWT first
    payload = decode_access_token(credentials.credentials)
    if payload:
        user_id = payload.get("sub", "")
        username = payload.get("username", "")
        if user_id:
            # Set Neo4j context for this request
            Neo4jDatabase.set_current_user(user_id)
            return CurrentUser(id=user_id, username=username, is_service=False)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_or_service(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """Verify JWT access token; fallback to service account via knowledge_api_token.

    This dependency is used by all existing API endpoints for backward compatibility
    with the MiroMind integration and other service-account callers.
    """
    settings = get_settings()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Try JWT first
    payload = decode_access_token(credentials.credentials)
    if payload:
        user_id = payload.get("sub", "")
        username = payload.get("username", "")
        if user_id:
            Neo4jDatabase.set_current_user(user_id)
            return CurrentUser(id=user_id, username=username, is_service=False)

    # Fallback: service account via knowledge_api_token
    expected = settings.knowledge_api_token
    if expected and hmac.compare_digest(credentials.credentials, expected):
        service_id = settings.default_user_id
        Neo4jDatabase.set_current_user(service_id)
        return CurrentUser(id=service_id, username="service", is_service=True)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_with_rate_limit(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Authentication + rate limit check. Use on user-facing endpoints."""
    if _rate_limiter and not current_user.is_service:
        path = str(request.url.path).lower()
        action = "ingest" if "/ingest" in path else "query"
        if not await _rate_limiter.acquire(current_user.id, action):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded for {action}. Please try again later.",
            )
    return current_user
