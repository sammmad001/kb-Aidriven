"""IngestTracker: tracks status of each ingest attempt as IngestRecord nodes in Neo4j."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.database import Neo4jDatabase

logger = logging.getLogger(__name__)

# Status constants
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

MAX_RETRY_COUNT = 3


class IngestTracker:
    """Manages IngestRecord nodes in Neo4j for ingest lifecycle tracking.

    Each IngestRecord node tracks:
      - source_id (unique): channel-specific identifier
      - source_type: "miromind" / "github" / "web" / ...
      - content_hash: SHA256 for dedup
      - status: pending → processing → completed / failed / skipped
      - kb_task_id: the KB IngestPipeline task_id
      - retry_count: how many times we've retried
      - error_msg: last error message
      - raw_path: path to persisted raw JSON for retry
    """

    def __init__(self, db: Neo4jDatabase) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Record lifecycle
    # ------------------------------------------------------------------

    async def record_attempt(
        self,
        source_id: str,
        source_type: str,
        content_hash: str,
        raw_path: str = "",
    ) -> dict[str, Any]:
        """Create or get existing IngestRecord. Returns dict with status info.

        If record already exists, returns existing status without modification.
        """
        existing = await self._get_record(source_id)
        if existing:
            logger.debug("IngestRecord already exists: %s (status=%s)",
                         source_id, existing.get("status"))
            return {
                "exists": True,
                "status": existing.get("status", STATUS_PENDING),
                "source_id": source_id,
            }

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute_write(
            """
            CREATE (r:IngestRecord {
                source_id: $source_id,
                source_type: $source_type,
                content_hash: $content_hash,
                status: $status,
                kb_task_id: '',
                retry_count: 0,
                error_msg: '',
                raw_path: $raw_path,
                created_at: $now,
                updated_at: $now
            })
            """,
            {
                "source_id": source_id,
                "source_type": source_type,
                "content_hash": content_hash,
                "status": STATUS_PENDING,
                "raw_path": raw_path,
                "now": now,
            },
        )
        logger.info("IngestRecord created: %s (type=%s)", source_id, source_type)
        return {
            "exists": False,
            "status": STATUS_PENDING,
            "source_id": source_id,
        }

    async def mark_processing(self, source_id: str) -> None:
        """Mark record as processing."""
        await self._update_status(source_id, STATUS_PROCESSING)

    async def mark_completed(
        self, source_id: str, kb_task_id: str = ""
    ) -> None:
        """Mark record as completed, optionally linking KB task_id."""
        await self._update_status(
            source_id, STATUS_COMPLETED, extra={"kb_task_id": kb_task_id}
        )
        logger.info("IngestRecord completed: %s (task=%s)", source_id, kb_task_id)

    async def mark_failed(self, source_id: str, error_msg: str) -> None:
        """Mark record as failed, incrementing retry_count."""
        record = await self._get_record(source_id)
        if not record:
            logger.warning("Cannot mark_failed: record not found: %s", source_id)
            return
        retry_count = int(record.get("retry_count", 0)) + 1
        # If exceeded max retries, keep as failed; scheduler won't pick it up
        await self._update_status(
            source_id, STATUS_FAILED,
            extra={"error_msg": error_msg, "retry_count": retry_count},
        )
        logger.warning("IngestRecord failed: %s (retry=%d/%d): %s",
                       source_id, retry_count, MAX_RETRY_COUNT, error_msg)

    async def mark_skipped(self, source_id: str, reason: str) -> None:
        """Mark record as skipped (e.g., below quality threshold)."""
        await self._update_status(
            source_id, STATUS_SKIPPED, extra={"error_msg": reason}
        )
        logger.info("IngestRecord skipped: %s (reason=%s)", source_id, reason)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def is_duplicate(self, content_hash: str) -> bool:
        """Check if content_hash already exists and was completed."""
        records = await self._db.execute_read(
            """
            MATCH (r:IngestRecord)
            WHERE r.content_hash = $hash AND r.status = $completed
            RETURN r.source_id LIMIT 1
            """,
            {"hash": content_hash, "completed": STATUS_COMPLETED},
        )
        return len(records) > 0

    async def get_failed_records(
        self, limit: int = 10, max_retries: int = MAX_RETRY_COUNT
    ) -> list[dict[str, Any]]:
        """Get records that need retry: status=failed and retry_count < max."""
        records = await self._db.execute_read(
            """
            MATCH (r:IngestRecord)
            WHERE r.status = $status
              AND r.retry_count < $max_retries
            RETURN r.source_id AS source_id,
                   r.source_type AS source_type,
                   r.content_hash AS content_hash,
                   r.retry_count AS retry_count,
                   r.raw_path AS raw_path,
                   r.error_msg AS error_msg
            ORDER BY r.updated_at ASC
            LIMIT $limit
            """,
            {"status": STATUS_FAILED, "max_retries": max_retries, "limit": limit},
        )
        return records

    async def get_stats(self) -> dict[str, int]:
        """Get aggregate statistics per status."""
        records = await self._db.execute_read(
            """
            MATCH (r:IngestRecord)
            RETURN r.status AS status, count(r) AS cnt
            """
        )
        stats: dict[str, int] = {
            STATUS_PENDING: 0,
            STATUS_PROCESSING: 0,
            STATUS_COMPLETED: 0,
            STATUS_FAILED: 0,
            STATUS_SKIPPED: 0,
        }
        for r in records:
            s = r.get("status", "")
            if s in stats:
                stats[s] = r.get("cnt", 0)
        stats["total"] = sum(stats.values())
        return stats

    async def get_record(self, source_id: str) -> dict[str, Any] | None:
        """Get a single record by source_id."""
        return await self._get_record(source_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_record(self, source_id: str) -> dict[str, Any] | None:
        records = await self._db.execute_read(
            "MATCH (r:IngestRecord {source_id: $source_id}) RETURN r",
            {"source_id": source_id},
        )
        if records:
            node = records[0].get("r")
            return dict(node) if node else None
        return None

    async def _update_status(
        self,
        source_id: str,
        status: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        set_clauses = ["r.status = $status", "r.updated_at = $now"]
        params: dict[str, Any] = {
            "source_id": source_id,
            "status": status,
            "now": now,
        }
        if extra:
            for key, val in extra.items():
                set_clauses.append(f"r.{key} = $extra_{key}")
                params[f"extra_{key}"] = val

        set_str = ", ".join(set_clauses)
        await self._db.execute_write(
            f"""
            MATCH (r:IngestRecord {{source_id: $source_id}})
            SET {set_str}
            """,
            params,
        )
