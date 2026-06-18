"""Tests for ResearchTracker: MiroMind research task persistence."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.research_tracker import (
    ResearchTracker,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
)


@pytest.fixture
def mock_db():
    """Mock Neo4j database with async methods."""
    db = MagicMock()
    db.execute_write_for_user = AsyncMock(return_value=[])
    db.execute_read_for_user = AsyncMock(return_value=[])
    db.execute_write = AsyncMock(return_value=[])
    db.get_current_user_id = MagicMock(return_value="test_user")
    return db


@pytest.fixture
def tracker(mock_db):
    return ResearchTracker(mock_db)


class TestResearchTrackerCreate:
    """Test create_task lifecycle entry point."""

    @pytest.mark.asyncio
    async def test_create_task_returns_uuid_hex(self, tracker, mock_db):
        """create_task should return a 32-char hex string."""
        task_id = await tracker.create_task("量子计算", "msg_123")
        assert len(task_id) == 32
        assert all(c in "0123456789abcdef" for c in task_id)

    @pytest.mark.asyncio
    async def test_create_task_calls_write(self, tracker, mock_db):
        """create_task should persist to Neo4j via execute_write_for_user."""
        await tracker.create_task("深度研究量子计算", "msg_abc")
        assert mock_db.execute_write_for_user.call_count == 1
        query, params = mock_db.execute_write_for_user.call_args[0]
        assert "CREATE" in query
        assert "ResearchTask" in query
        assert params["question"] == "深度研究量子计算"
        assert params["message_id"] == "msg_abc"
        assert params["status"] == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_create_task_unique_ids(self, tracker):
        """Each create_task call should return a unique task_id."""
        id1 = await tracker.create_task("Q1", "m1")
        id2 = await tracker.create_task("Q2", "m2")
        assert id1 != id2


class TestResearchTrackerLifecycle:
    """Test mark_running, mark_completed, mark_failed transitions."""

    @pytest.mark.asyncio
    async def test_mark_running(self, tracker, mock_db):
        """mark_running should update status and set started_at."""
        await tracker.mark_running("task_001")
        assert mock_db.execute_write_for_user.call_count == 1
        query, params = mock_db.execute_write_for_user.call_args[0]
        assert "SET r.status" in query
        assert params["status"] == STATUS_RUNNING
        assert params["task_id"] == "task_001"

    @pytest.mark.asyncio
    async def test_mark_completed(self, tracker, mock_db):
        """mark_completed should store all result fields."""
        await tracker.mark_completed(
            "task_001",
            content="量子计算是一种利用量子力学原理的计算技术...",
            model="mirothinker-1-7-deepresearch",
            total_tokens=1500,
            duration_ms=30000,
            ingest_summary="新增 3 节点",
        )
        query, params = mock_db.execute_write_for_user.call_args[0]
        assert params["status"] == STATUS_COMPLETED
        assert params["content"] == "量子计算是一种利用量子力学原理的计算技术..."
        assert params["model"] == "mirothinker-1-7-deepresearch"
        assert params["tokens"] == 1500
        assert params["ingest_summary"] == "新增 3 节点"

    @pytest.mark.asyncio
    async def test_mark_completed_truncates_long_content(self, tracker, mock_db):
        """Content exceeding MAX_CONTENT_STORE should be truncated."""
        long_content = "X" * 10000
        await tracker.mark_completed(
            "task_001",
            content=long_content,
            model="m",
            total_tokens=0,
            duration_ms=0,
        )
        _, params = mock_db.execute_write_for_user.call_args[0]
        assert len(params["content"]) == 5000

    @pytest.mark.asyncio
    async def test_mark_failed(self, tracker, mock_db):
        """mark_failed should set error message and status."""
        await tracker.mark_failed("task_001", "API timeout after 300s")
        query, params = mock_db.execute_write_for_user.call_args[0]
        assert params["status"] == STATUS_FAILED
        assert params["error"] == "API timeout after 300s"
        assert params["task_id"] == "task_001"


class TestResearchTrackerQueries:
    """Test get_task and get_recent_tasks."""

    @pytest.mark.asyncio
    async def test_get_task_found(self, tracker, mock_db):
        """get_task should return node dict when found."""
        mock_db.execute_read_for_user.return_value = [
            {"r": {"task_id": "t1", "status": STATUS_COMPLETED, "question": "量子计算"}}
        ]
        result = await tracker.get_task("t1")
        assert result is not None
        assert result["task_id"] == "t1"
        assert result["status"] == STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, tracker, mock_db):
        """get_task should return None when not found."""
        mock_db.execute_read_for_user.return_value = []
        result = await tracker.get_task("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_recent_tasks(self, tracker, mock_db):
        """get_recent_tasks should return list ordered by created_at DESC."""
        mock_db.execute_read_for_user.return_value = [
            {"task_id": "t2", "question": "第二题", "status": STATUS_RUNNING},
            {"task_id": "t1", "question": "第一题", "status": STATUS_COMPLETED},
        ]
        results = await tracker.get_recent_tasks(limit=5)
        assert len(results) == 2
        assert results[0]["task_id"] == "t2"
        assert results[1]["task_id"] == "t1"

        # Verify LIMIT parameter
        _, params = mock_db.execute_read_for_user.call_args[0]
        assert params["limit"] == 5


class TestResearchTrackerRecovery:
    """Test recover_interrupted for startup recovery."""

    @pytest.mark.asyncio
    async def test_recover_interrupted_marks_tasks_failed(self, tracker, mock_db):
        """recover_interrupted should mark running/pending as failed."""
        mock_db.execute_write.return_value = [{"recovered": 3}]
        count = await tracker.recover_interrupted()
        assert count == 3

        query, params = mock_db.execute_write.call_args[0]
        assert "running" in query.lower() or "pending" in query.lower()
        assert params["failed"] == STATUS_FAILED
        assert "重启" in params["error_msg"]

    @pytest.mark.asyncio
    async def test_recover_interrupted_zero_tasks(self, tracker, mock_db):
        """recover_interrupted should return 0 when no stuck tasks."""
        mock_db.execute_write.return_value = [{"recovered": 0}]
        count = await tracker.recover_interrupted()
        assert count == 0

    @pytest.mark.asyncio
    async def test_recover_interrupted_uses_execute_write_not_user_scoped(self, tracker, mock_db):
        """recover_interrupted must use execute_write (cross-user), not execute_write_for_user."""
        await tracker.recover_interrupted()
        assert mock_db.execute_write.call_count == 1
        assert mock_db.execute_write_for_user.call_count == 0


class TestResearchTrackerErrorHandling:
    """Test that tracker methods don't crash on DB errors (failures are non-fatal)."""

    @pytest.mark.asyncio
    async def test_create_task_db_error_propagates(self, tracker, mock_db):
        """DB errors should propagate (handlers wrap in try/except)."""
        mock_db.execute_write_for_user.side_effect = Exception("DB connection lost")
        with pytest.raises(Exception, match="DB connection lost"):
            await tracker.create_task("Q", "m")
