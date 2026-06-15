"""SQLite-based user store using aiosqlite."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import aiosqlite

from app.auth.password import hash_password, verify_password
from app.config import Settings

logger = logging.getLogger(__name__)

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    is_service  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feishu_user_mappings (
    open_id     TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    feishu_name TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS token_usage_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    tokens_used INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class UserStore:
    """Async user data store backed by SQLite."""

    def __init__(self, settings: Settings) -> None:
        self._db_path = settings.user_db_path
        self._default_user_id = settings.default_user_id
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open connection and create tables. Idempotent."""
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_CREATE_TABLES_SQL)
        await self._conn.commit()
        logger.info("UserStore initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "UserStore not initialized"
        return self._conn

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    async def create_user(self, username: str, password: str) -> dict:
        """Create a new user. Returns user dict. Raises on duplicate username."""
        user_id = f"usr_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:16]}"
        pwd_hash = hash_password(password)
        try:
            await self.conn.execute_insert(
                "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
                (user_id, username, pwd_hash),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            raise ValueError(f"Username '{username}' already exists")
        return {"id": user_id, "username": username, "is_service": 0}

    async def get_user_by_username(self, username: str) -> dict | None:
        """Fetch a user by username."""
        async with self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_by_id(self, user_id: str) -> dict | None:
        """Fetch a user by internal id."""
        async with self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def verify_user(self, username: str, password: str) -> dict | None:
        """Verify username+password. Returns user dict on success, None on failure."""
        user = await self.get_user_by_username(username)
        if not user:
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return user

    async def ensure_service_account(self, user_id: str) -> None:
        """Ensure the service account exists (for knowledge_api_token fallback)."""
        existing = await self.get_user_by_id(user_id)
        if not existing:
            await self.conn.execute_insert(
                "INSERT OR IGNORE INTO users (id, username, password_hash, is_service) VALUES (?, ?, ?, 1)",
                (user_id, user_id, hash_password("__service_account__")),
            )
            await self._conn.commit()
            logger.info("Service account created: %s", user_id)

    async def ensure_default_user(self) -> None:
        """Ensure the default user exists (for migration target)."""
        existing = await self.get_user_by_id(self._default_user_id)
        if not existing:
            await self.conn.execute_insert(
                "INSERT OR IGNORE INTO users (id, username, password_hash, is_service) VALUES (?, ?, ?, 1)",
                (self._default_user_id, self._default_user_id, hash_password("__default__")),
            )
            await self._conn.commit()
            logger.info("Default user created: %s", self._default_user_id)

    # ------------------------------------------------------------------
    # Feishu user mapping
    # ------------------------------------------------------------------

    async def get_or_create_feishu_user(self, open_id: str, feishu_name: str = "") -> dict:
        """Map a Feishu open_id to a kb user. Creates the user if first time."""
        async with self.conn.execute(
            "SELECT user_id FROM feishu_user_mappings WHERE open_id = ?", (open_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                # Update display name if provided
                if feishu_name:
                    await self.conn.execute(
                        "UPDATE feishu_user_mappings SET feishu_name = ? WHERE open_id = ?",
                        (feishu_name, open_id),
                    )
                    await self._conn.commit()
                return await self.get_user_by_id(row["user_id"]) or {}

        # First-time Feishu user: auto-create
        username = f"feishu_{open_id[:12]}"
        user = await self.create_user(username, open_id + "_random_pwd_2024")
        await self.conn.execute_insert(
            "INSERT INTO feishu_user_mappings (open_id, user_id, feishu_name) VALUES (?, ?, ?)",
            (open_id, user["id"], feishu_name),
        )
        await self._conn.commit()
        logger.info("Feishu user mapped: %s -> %s", open_id, user["id"])
        return user
