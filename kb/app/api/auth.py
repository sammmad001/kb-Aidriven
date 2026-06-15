"""Authentication API endpoints: register, login, refresh, me."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.deps import (
    _user_store,
    get_current_user,
    set_user_store,
)
from app.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from app.config import get_settings
from app.models import (
    CurrentUser,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserLogin,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_store():
    """Get the global UserStore instance."""
    assert _user_store is not None, "UserStore not initialized"
    return _user_store


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: UserCreate) -> TokenResponse:
    """Register a new user and return tokens."""
    store = _get_store()
    try:
        user = await store.create_user(body.username, body.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    access = create_access_token(user["id"], user["username"])
    refresh = create_refresh_token(user["id"], user["username"])
    settings = get_settings()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin) -> TokenResponse:
    """Login with username/password and return tokens."""
    store = _get_store()
    user = await store.verify_user(body.username, body.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    access = create_access_token(user["id"], user["username"])
    refresh = create_refresh_token(user["id"], user["username"])
    settings = get_settings()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest) -> TokenResponse:
    """Exchange a refresh token for a new access+refresh token pair."""
    payload = decode_refresh_token(body.refresh_token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user_id = payload.get("sub", "")
    username = payload.get("username", "")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed refresh token",
        )

    access = create_access_token(user_id, username)
    refresh = create_refresh_token(user_id, username)
    settings = get_settings()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=CurrentUser)
async def me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Return the current authenticated user info."""
    return current_user
