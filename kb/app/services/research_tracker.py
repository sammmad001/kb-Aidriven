"""ResearchTracker: persists MiroMind deep research tasks in Neo4j.

Solves the "fire-and-forget" problem where asyncio.create_task() tasks are
lost on server restart. Each research task is stored as a ResearchTask node
with full lifecycle tracking (pending → running → completed/failed).

On startup, recover_interrupted() marks any stuck "running" tasks as failed
so users get clear feedback instead of waiting forever.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.database import Neo4jDatabase

logger = logging.getLogger(__name__)

# Status constants
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Maximum content length stored in Neo4j (avoid bloating the graph)
MAX_CONTENT_STORE = 5000


class ResearchTracker:
    """Manages ResearchTask nodes in Neo4j for research lifecycle tracking.

    Lifecycle:
        pending → running → completed (success)
                           → failed    (API error / timeout / server restart)

    The tracker is user-scoped: each ResearchTask node carries a user_id
    and queries are automatically filtered by the current user context.
    """

    def __init__(self, db: Neo4jDatabase) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    async def create_task(
        self,
        question: str,
        message_id: str,
    ) -> str:
        """Create a new ResearchTask record. Returns the task_id.

        Args:
            question: The research question.
            message_id: Feishu message_id for reply correlation.

        Returns:
            UUID hex string task_id.
        """
        task_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute_write_for_user(
            """
            CREATE (r:ResearchTask {
                task_id: $task_id,
                question: $question,
                message_id: $message_id,
                status: $status,
                user_id: $_user_id,
                created_at: $now,
                updated_at: $now,
                started_at: '',
                completed_at: '',
                result_content: '',
                result_model: '',
                result_tokens: 0,
                duration_ms: 0,
                error: '',
                ingest_summary: ''
            })
            """,
            {
                "task_id": task_id,
                "question": question,
                "message_id": message_id,
                "status": STATUS_PENDING,
                "now": now,
            },
        )
        logger.info("ResearchTask created: %s (question=%s...)", task_id, question[:50])
        return task_id

    async def mark_running(self, task_id: str) -> None:
        """Mark task as running (API call in progress)."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute_write_for_user(
            """
            MATCH (r:ResearchTask {task_id: $task_id})
            WHERE r.user_id = $_user_id
            SET r.status = $status,
                r.started_at = $now,
                r.updated_at = $now
            """,
            {"task_id": task_id, "status": STATUS_RUNNING, "now": now},
        )
        logger.info("ResearchTask running: %s", task_id)

    async def mark_completed(
        self,
        task_id: str,
        content: str,
        model: str,
        total_tokens: int,
        duration_ms: int,
        ingest_summary: str = "",
    ) -> None:
        """Mark task as completed with result data."""
        now = datetime.now(timezone.utc).isoformat()
        # Truncate content to avoid bloating Neo4j
        stored_content = content[:MAX_CONTENT_STORE] if content else ""
        await self._db.execute_write_for_user(
            """
            MATCH (r:ResearchTask {task_id: $task_id})
            WHERE r.user_id = $_user_id
            SET r.status = $status,
                r.result_content = $content,
                r.result_model = $model,
                r.result_tokens = $tokens,
                r.duration_ms = $duration_ms,
                r.ingest_summary = $ingest_summary,
                r.completed_at = $now,
                r.updated_at = $now
            """,
            {
                "task_id": task_id,
                "status": STATUS_COMPLETED,
                "content": stored_content,
                "model": model,
                "tokens": total_tokens,
                "duration_ms": duration_ms,
                "ingest_summary": ingest_summary,
                "now": now,
            },
        )
        logger.info(
            "ResearchTask completed: %s (tokens=%d duration=%dms)",
            task_id, total_tokens, duration_ms,
        )

    async def mark_failed(self, task_id: str, error: str) -> None:
        """Mark task as failed with error message."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute_write_for_user(
            """
            MATCH (r:ResearchTask {task_id: $task_id})
            WHERE r.user_id = $_user_id
            SET r.status = $status,
                r.error = $error,
                r.completed_at = $now,
                r.updated_at = $now
            """,
            {"task_id": task_id, "status": STATUS_FAILED, "error": error, "now": now},
        )
        logger.warning("ResearchTask failed: %s (error=%s)", task_id, error[:100])

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a single research task by task_id."""
        records = await self._db.execute_read_for_user(
            """
            MATCH (r:ResearchTask {task_id: $task_id})
            WHERE r.user_id = $_user_id
            RETURN r
            """,
            {"task_id": task_id},
        )
        if records:
            node = records[0].get("r")
            return dict(node) if node else None
        return None

    async def get_recent_tasks(self, limit: int = 5) -> list[dict[str, Any]]:
        """Get recent research tasks for the current user, newest first."""
        records = await self._db.execute_read_for_user(
            """
            MATCH (r:ResearchTask)
            WHERE r.user_id = $_user_id
            RETURN r.task_id AS task_id,
                   r.question AS question,
                   r.status AS status,
                   r.created_at AS created_at,
                   r.completed_at AS completed_at,
                   r.result_tokens AS result_tokens,
                   r.duration_ms AS duration_ms,
                   r.error AS error,
                   r.ingest_summary AS ingest_summary
            ORDER BY r.created_at DESC
            LIMIT $limit
            """,
            {"limit": limit},
        )
        return records

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def recover_interrupted(self) -> int:
        """Mark any stuck 'running' or 'pending' tasks as failed.

        Called on application startup. Tasks stuck in running/pending
        indicate a server crash/restart while the API call was in flight.

        Returns the number of recovered (marked failed) tasks.
        """
        now = datetime.now(timezone.utc).isoformat()
        records = await self._db.execute_write(
            """
            MATCH (r:ResearchTask)
            WHERE r.status IN ['running', 'pending']
            SET r.status = $failed,
                r.error = $error_msg,
                r.completed_at = $now,
                r.updated_at = $now
            RETURN count(r) AS recovered
            """,
            {
                "failed": STATUS_FAILED,
                "error_msg": "服务重启中断 — 任务未完成",
                "now": now,
            },
        )
        count = 0
        if records:
            count = records[0].get("recovered", 0) or 0
        if count > 0:
            logger.warning("Recovered %d interrupted research tasks on startup", count)
        else:
            logger.info("No interrupted research tasks to recover")
        return count
