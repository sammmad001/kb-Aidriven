"""SQLite-based user store using aiosqlite."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

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

    # ------------------------------------------------------------------
    # Account binding (unify Web + Feishu accounts)
    # ------------------------------------------------------------------

    async def get_feishu_user(self, open_id: str) -> dict | None:
        """Look up the Feishu mapping *without* auto-creating.

        Returns the user dict or None if open_id is not yet bound.
        """
        async with self.conn.execute(
            "SELECT u.* FROM feishu_user_mappings m "
            "JOIN users u ON m.user_id = u.id WHERE m.open_id = ?",
            (open_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_feishu_binding_status(self, open_id: str) -> dict:
        """Return a binding-status dict.

        ``{"bound": bool, "user_id": str|None, "username": str|None}``
        """
        user = await self.get_feishu_user(open_id)
        if user:
            return {
                "bound": True,
                "user_id": user["id"],
                "username": user.get("username", ""),
            }
        return {"bound": False, "user_id": None, "username": None}

    async def bind_feishu_user(
        self,
        open_id: str,
        username: str,
        password: str,
        feishu_name: str = "",
        db: Any = None,
    ) -> dict:
        """Bind a Feishu *open_id* to an existing Web account.

        Verifies the Web credentials, migrates any Neo4j data previously
        owned by an auto-created ``feishu_*`` account, then UPSERTs the
        mapping row.

        Returns ``{"success": True, ...}`` or ``{"success": False, "error": ...}``.
        """
        # 1. Verify Web account credentials
        user = await self.verify_user(username, password)
        if not user:
            return {"success": False, "error": "invalid_credentials"}

        new_user_id = user["id"]
        migrated_nodes = 0

        # 2. Check for an existing (auto-created) mapping to migrate from
        async with self.conn.execute(
            "SELECT user_id FROM feishu_user_mappings WHERE open_id = ?",
            (open_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                old_user_id = row["user_id"]
                # 3. Migrate Neo4j data if the old account differs
                if old_user_id != new_user_id and db:
                    migrated_nodes = await self._migrate_neo4j_data(
                        old_user_id, new_user_id, db,
                    )

        # 4. UPSERT mapping (INSERT OR REPLACE for SQLite)
        await self.conn.execute(
            "INSERT INTO feishu_user_mappings (open_id, user_id, feishu_name) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(open_id) DO UPDATE SET user_id = ?, feishu_name = ?",
            (open_id, new_user_id, feishu_name, new_user_id, feishu_name),
        )
        await self._conn.commit()

        logger.info(
            "Feishu user bound: %s... -> %s (migrated %d nodes)",
            open_id[:8], new_user_id, migrated_nodes,
        )
        return {
            "success": True,
            "user_id": new_user_id,
            "username": username,
            "migrated_nodes": migrated_nodes,
        }

    async def register_feishu_user(
        self,
        open_id: str,
        username: str,
        password: str,
        feishu_name: str = "",
        db: Any = None,
    ) -> dict:
        """Register a brand-new account from Feishu and auto-bind.

        Unlike ``bind_feishu_user`` (which binds to an *existing* Web account),
        this method creates the account first, then delegates to
        ``bind_feishu_user`` for mapping + data migration.

        Returns ``{"success": True, ...}`` or ``{"success": False, "error": ...}``.
        """
        # 1. Reject if this open_id is already bound to a *real* Web account.
        # Auto-created feishu_* accounts (old flow) are allowed — they will
        # be overridden by bind_feishu_user with data migration.
        binding = await self.get_feishu_binding_status(open_id)
        if binding["bound"]:
            bound_username = binding.get("username", "")
            if not bound_username.startswith("feishu_"):
                return {
                    "success": False,
                    "error": "already_bound",
                    "username": bound_username,
                }

        # 2. Reject if username is taken
        existing = await self.get_user_by_username(username)
        if existing:
            return {"success": False, "error": "username_exists"}

        # 3. Create the new Web account
        try:
            await self.create_user(username, password)
        except ValueError:
            return {"success": False, "error": "username_exists"}

        # 4. Delegate to bind_feishu_user for mapping + migration
        return await self.bind_feishu_user(
            open_id, username, password, feishu_name=feishu_name, db=db,
        )

    async def unbind_feishu_user(self, open_id: str) -> dict:
        """Remove the Feishu binding for *open_id*."""
        await self.conn.execute(
            "DELETE FROM feishu_user_mappings WHERE open_id = ?",
            (open_id,),
        )
        await self._conn.commit()
        logger.info("Feishu user unbound: %s...", open_id[:8])
        return {"success": True}

    # ------------------------------------------------------------------
    # Neo4j data migration (two-phase, idempotent)
    # ------------------------------------------------------------------

    @staticmethod
    async def _migrate_neo4j_data(
        old_user_id: str, new_user_id: str, db: Any,
    ) -> int:
        """Migrate Neo4j nodes from *old_user_id* to *new_user_id*.

        Phase 1 — **merge same-name nodes**: when both accounts have a node
        with the same ``name``, absorb the old node's metadata (aliases,
        tags, summary) into the new node, then ``DETACH DELETE`` the old
        node (its relationships are lost; the new node already has its own).

        Phase 2 — **direct migration**: all remaining nodes belonging to
        *old_user_id* are re-assigned via ``SET n.user_id = $new_uid``,
        preserving their relationships.

        Returns the total number of nodes processed (merged + migrated).
        """
        # --- Phase 1: merge same-name nodes ---
        merge_query = """
            MATCH (old) WHERE old.user_id = $old_uid AND (old:Entity OR old:Concept)
            MATCH (new) WHERE new.user_id = $new_uid AND old.name = new.name
            // Absorb old's aliases into new (deduped)
            SET new.aliases = coalesce(new.aliases, [])
                + [a IN coalesce(old.aliases, []) WHERE NOT a IN coalesce(new.aliases, [])]
            // Absorb old's tags into new
            SET new.tags = coalesce(new.tags, []) + coalesce(old.tags, [])
            // Fill empty summary from old
            SET new.summary = CASE
                WHEN new.summary IS NULL OR new.summary = '' THEN old.summary
                ELSE new.summary
            END
            WITH collect(DISTINCT old) AS oldNodes
            UNWIND oldNodes AS o
            DETACH DELETE o
            RETURN count(o) AS merged_count
        """
        try:
            results = await db.execute_write(merge_query, {
                "old_uid": old_user_id,
                "new_uid": new_user_id,
            })
            merged = results[0].get("merged_count", 0) if results else 0
        except Exception as exc:
            logger.warning("Phase 1 merge failed (continuing to Phase 2): %s", exc)
            merged = 0

        # --- Phase 2: directly re-assign remaining nodes ---
        migrate_query = """
            MATCH (n) WHERE n.user_id = $old_uid
            SET n.user_id = $new_uid
            RETURN count(n) AS migrated_count
        """
        try:
            results = await db.execute_write(migrate_query, {
                "old_uid": old_user_id,
                "new_uid": new_user_id,
            })
            migrated = results[0].get("migrated_count", 0) if results else 0
        except Exception as exc:
            logger.warning("Phase 2 migration failed: %s", exc)
            migrated = 0

        total = merged + migrated
        logger.info(
            "Neo4j migration: %s -> %s (merged=%d, migrated=%d)",
            old_user_id, new_user_id, merged, migrated,
        )
        return total
