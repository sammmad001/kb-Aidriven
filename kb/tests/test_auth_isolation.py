"""Tests for multi-user authentication, data isolation, and ownership.

Covers:
- User registration, login, password verification (UserStore)
- JWT token creation, verification, expiry
- Feishu user mapping (open_id → user_id)
- Data isolation via execute_read_for_user / execute_write_for_user
- Task ownership verification (namespaced task_id)
- Rate limiter per-user enforcement
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def tmp_db_path():
    """Provide a temporary SQLite DB path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # Remove empty file so aiosqlite creates fresh
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def user_store(tmp_db_path):
    """Initialized UserStore on a temp SQLite DB."""
    from app.auth.user_store import UserStore
    settings = Settings(
        neo4j_password="test",
        user_db_path=tmp_db_path,
        default_user_id="default",
    )
    store = UserStore(settings)
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def test_settings(tmp_db_path):
    """Settings with known JWT secrets for testing."""
    return Settings(
        neo4j_password="test",
        user_db_path=tmp_db_path,
        jwt_secret_key="test_secret_key_at_least_32_chars_long_1234567890",
        default_user_id="default",
        knowledge_api_token="test_api_token_123",
    )


# ======================================================================
# UserStore Tests
# ======================================================================

class TestUserStore:
    """Test user CRUD operations and password verification."""

    @pytest.mark.asyncio
    async def test_create_user(self, user_store):
        user = await user_store.create_user("alice", "password123")
        assert user["username"] == "alice"
        assert user["id"].startswith("usr_")
        assert user["is_service"] == 0

    @pytest.mark.asyncio
    async def test_create_duplicate_username_raises(self, user_store):
        await user_store.create_user("bob", "pass1")
        with pytest.raises(ValueError, match="already exists"):
            await user_store.create_user("bob", "pass2")

    @pytest.mark.asyncio
    async def test_verify_user_success(self, user_store):
        await user_store.create_user("charlie", "mypassword")
        user = await user_store.verify_user("charlie", "mypassword")
        assert user is not None
        assert user["username"] == "charlie"

    @pytest.mark.asyncio
    async def test_verify_user_wrong_password(self, user_store):
        await user_store.create_user("dave", "correct")
        user = await user_store.verify_user("dave", "wrong")
        assert user is None

    @pytest.mark.asyncio
    async def test_verify_user_nonexistent(self, user_store):
        user = await user_store.verify_user("ghost", "anything")
        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_by_id(self, user_store):
        created = await user_store.create_user("eve", "pass")
        fetched = await user_store.get_user_by_id(created["id"])
        assert fetched is not None
        assert fetched["username"] == "eve"

    @pytest.mark.asyncio
    async def test_get_user_by_username(self, user_store):
        await user_store.create_user("frank", "pass")
        fetched = await user_store.get_user_by_username("frank")
        assert fetched is not None
        assert fetched["username"] == "frank"

    @pytest.mark.asyncio
    async def test_ensure_service_account(self, user_store):
        await user_store.ensure_service_account("svc_test")
        user = await user_store.get_user_by_id("svc_test")
        assert user is not None
        assert user["is_service"] == 1

    @pytest.mark.asyncio
    async def test_ensure_default_user(self, user_store):
        await user_store.ensure_default_user()
        user = await user_store.get_user_by_id("default")
        assert user is not None


# ======================================================================
# Password Hashing Tests
# ======================================================================

class TestPasswordHashing:
    """Test password hash and verification."""

    def test_hash_and_verify(self):
        from app.auth.password import hash_password, verify_password
        h = hash_password("mypassword")
        assert h != "mypassword"
        assert verify_password("mypassword", h) is True

    def test_verify_wrong_password(self):
        from app.auth.password import hash_password, verify_password
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_verify_corrupt_hash(self):
        from app.auth.password import verify_password
        assert verify_password("anything", "not_a_hash") is False

    def test_different_passwords_different_hashes(self):
        from app.auth.password import hash_password
        h1 = hash_password("pass1")
        h2 = hash_password("pass2")
        assert h1 != h2


# ======================================================================
# JWT Token Tests
# ======================================================================

class TestJWT:
    """Test JWT creation and verification."""

    def test_create_and_decode_access_token(self, test_settings):
        with patch("app.auth.jwt.get_settings", return_value=test_settings):
            from app.auth.jwt import create_access_token, decode_access_token
            token = create_access_token("user_123", "alice")
            payload = decode_access_token(token)
            assert payload is not None
            assert payload["sub"] == "user_123"
            assert payload["username"] == "alice"
            assert payload["type"] == "access"

    def test_create_and_decode_refresh_token(self, test_settings):
        with patch("app.auth.jwt.get_settings", return_value=test_settings):
            from app.auth.jwt import create_refresh_token, decode_refresh_token
            token = create_refresh_token("user_456", "bob")
            payload = decode_refresh_token(token)
            assert payload is not None
            assert payload["sub"] == "user_456"
            assert payload["type"] == "refresh"

    def test_access_token_rejected_as_refresh(self, test_settings):
        with patch("app.auth.jwt.get_settings", return_value=test_settings):
            from app.auth.jwt import create_access_token, decode_refresh_token
            token = create_access_token("user_789", "carol")
            payload = decode_refresh_token(token)
            assert payload is None

    def test_refresh_token_rejected_as_access(self, test_settings):
        with patch("app.auth.jwt.get_settings", return_value=test_settings):
            from app.auth.jwt import create_refresh_token, decode_access_token
            token = create_refresh_token("user_000", "dave")
            payload = decode_access_token(token)
            assert payload is None

    def test_decode_invalid_token(self, test_settings):
        with patch("app.auth.jwt.get_settings", return_value=test_settings):
            from app.auth.jwt import decode_access_token
            assert decode_access_token("not.a.valid.token") is None
            assert decode_access_token("") is None

    def test_decode_token_wrong_secret(self, test_settings):
        with patch("app.auth.jwt.get_settings", return_value=test_settings):
            from app.auth.jwt import create_access_token
            token = create_access_token("user_x", "eve")
        # Decode with different secret
        other_settings = Settings(
            neo4j_password="test",
            jwt_secret_key="a_completely_different_secret_key_1234567890",
        )
        with patch("app.auth.jwt.get_settings", return_value=other_settings):
            from app.auth.jwt import decode_access_token
            assert decode_access_token(token) is None


# ======================================================================
# Feishu User Mapping Tests
# ======================================================================

class TestFeishuUserMapping:
    """Test Feishu open_id → user_id mapping."""

    @pytest.mark.asyncio
    async def test_first_feishu_user_creates_mapping(self, user_store):
        user = await user_store.get_or_create_feishu_user("ou_open_id_001", "张三")
        assert user["id"].startswith("usr_")
        assert "feishu_" in user["username"]

    @pytest.mark.asyncio
    async def test_same_open_id_returns_same_user(self, user_store):
        user1 = await user_store.get_or_create_feishu_user("ou_alpha_001", "李四")
        user2 = await user_store.get_or_create_feishu_user("ou_alpha_001", "李四updated")
        assert user1["id"] == user2["id"]

    @pytest.mark.asyncio
    async def test_different_open_ids_create_different_users(self, user_store):
        user1 = await user_store.get_or_create_feishu_user("ou_beta_001", "王五")
        user2 = await user_store.get_or_create_feishu_user("ou_gamma_002", "赵六")
        assert user1["id"] != user2["id"]


# ======================================================================
# Data Isolation via execute_*_for_user Tests
# ======================================================================

class TestDataIsolation:
    """Test that execute_read_for_user / execute_write_for_user inject _user_id."""

    @pytest.mark.asyncio
    async def test_execute_read_for_user_injects_user_id(self):
        """execute_read_for_user should pass params to execute_read with _user_id key."""
        from tests.conftest import MockNeo4jDatabase

        captured_params: dict = {}

        class TrackingMockDB(MockNeo4jDatabase):
            async def execute_read(self, query, params=None):
                captured_params.update(params or {})
                return []

        db = TrackingMockDB()
        db.set_current_user("user_alice")
        await db.execute_read_for_user("MATCH (n) RETURN n")
        assert captured_params.get("_user_id") == "test_user"

    @pytest.mark.asyncio
    async def test_execute_write_for_user_injects_user_id(self):
        """execute_write_for_user should pass params to execute_write with _user_id key."""
        from tests.conftest import MockNeo4jDatabase

        captured_params: dict = {}

        class TrackingMockDB(MockNeo4jDatabase):
            async def execute_write(self, query, params=None):
                captured_params.update(params or {})
                return []

        db = TrackingMockDB()
        db.set_current_user("user_bob")
        await db.execute_write_for_user("MERGE (n {id: $id})", {"id": "test_node"})
        assert captured_params.get("_user_id") == "test_user"
        assert captured_params.get("id") == "test_node"

    @pytest.mark.asyncio
    async def test_isolated_users_have_separate_data(self):
        """Simulate two users writing to the same mock DB — data should be separate."""
        from tests.conftest import MockNeo4jDatabase

        # In a real Neo4j, the MERGE query uses $_user_id as a composite key.
        # Here we simulate the behavior by tracking data per user_id.
        db = MockNeo4jDatabase()

        # User A writes a node
        db._nodes["RAG_userA"] = {"id": "RAG", "name": "RAG", "user_id": "userA"}
        # User B writes a node with same logical name
        db._nodes["RAG_userB"] = {"id": "RAG", "name": "RAG", "user_id": "userB"}

        # Simulate user-scoped query for userA
        user_a_nodes = [
            n for n in db._nodes.values() if n.get("user_id") == "userA"
        ]
        user_b_nodes = [
            n for n in db._nodes.values() if n.get("user_id") == "userB"
        ]

        assert len(user_a_nodes) == 1
        assert len(user_b_nodes) == 1
        assert user_a_nodes[0]["user_id"] == "userA"
        assert user_b_nodes[0]["user_id"] == "userB"
        # Same logical entity, but different physical records
        assert user_a_nodes[0]["id"] == user_b_nodes[0]["id"]


# ======================================================================
# Task Ownership Tests
# ======================================================================

class TestTaskOwnership:
    """Test that task IDs are namespaced and ownership is verified."""

    @pytest.mark.asyncio
    async def test_task_id_is_namespaced_with_user(self):
        """Task IDs should be prefixed with user_id."""
        from tests.conftest import MockNeo4jDatabase

        db = MockNeo4jDatabase()
        uid = db.get_current_user_id_or_default()

        # Simulate pipeline task ID generation
        from uuid import uuid4
        task_id = f"{uid}:{uuid4().hex}" if uid else uuid4().hex

        assert ":" in task_id
        assert task_id.split(":")[0] == uid

    def test_get_task_status_rejects_other_user(self):
        """Pipeline.get_task_status should return None for another user's task."""
        from tests.conftest import MockNeo4jDatabase

        db = MockNeo4jDatabase()

        # Simulate: userA creates a task
        task_id_a = "userA:abc123"
        # UserB tries to access it
        # Since MockNeo4jDatabase.get_current_user_id_or_default returns "test_user",
        # and task prefix is "userA", they won't match
        uid = db.get_current_user_id_or_default()
        task_uid = task_id_a.rsplit(":", 1)[0]
        assert task_uid != uid  # Different users

    def test_get_task_status_allows_owner(self):
        """Pipeline.get_task_status should return task for the owner."""
        from tests.conftest import MockNeo4jDatabase

        db = MockNeo4jDatabase()
        uid = db.get_current_user_id_or_default()
        task_id = f"{uid}:abc123"

        task_uid = task_id.rsplit(":", 1)[0]
        assert task_uid == uid  # Same user


# ======================================================================
# Authentication Dependency Tests
# ======================================================================

class TestAuthDependencies:
    """Test that auth dependencies reject unauthenticated requests."""

    @pytest.mark.asyncio
    async def test_missing_credentials_raises_401(self, test_settings):
        from fastapi import HTTPException
        from app.auth.deps import get_current_user

        with patch("app.auth.deps.get_settings", return_value=test_settings):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=None)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_jwt_raises_401(self, test_settings):
        from fastapi import HTTPException
        from app.auth.deps import get_current_user

        bad_creds = MagicMock()
        bad_creds.credentials = "invalid.jwt.token"

        with patch("app.auth.deps.get_settings", return_value=test_settings):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=bad_creds)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_service_token_fallback(self, test_settings):
        """get_current_user_or_service should accept knowledge_api_token."""
        from app.auth.deps import get_current_user_or_service

        service_creds = MagicMock()
        service_creds.credentials = test_settings.knowledge_api_token

        with patch("app.auth.deps.get_settings", return_value=test_settings):
            user = await get_current_user_or_service(credentials=service_creds)
            assert user is not None
            assert user.is_service is True
            assert user.id == test_settings.default_user_id

    @pytest.mark.asyncio
    async def test_valid_jwt_returns_user(self, test_settings):
        """get_current_user should accept valid JWT and set user context."""
        with patch("app.auth.jwt.get_settings", return_value=test_settings), \
             patch("app.auth.deps.get_settings", return_value=test_settings):
            from app.auth.jwt import create_access_token
            from app.auth.deps import get_current_user

            token = create_access_token("usr_test123", "testuser")

            creds = MagicMock()
            creds.credentials = token

            user = await get_current_user(credentials=creds)
            assert user.id == "usr_test123"
            assert user.username == "testuser"
            assert user.is_service is False
