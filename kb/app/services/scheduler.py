"""APScheduler-based retry scheduler for failed ingest records."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class IngestRetryScheduler:
    """Periodically scans Neo4j for failed IngestRecord nodes and retries them.

    Works alongside the event-driven path (MiroMind hook → POST /api/ingest/miromind).
    This handles cases where data reached KB but processing failed (LLM timeout,
    Neo4j errors, etc.).
    """

    def __init__(
        self,
        tracker,  # IngestTracker
        pipeline,  # IngestPipeline
        raw_ingest_dir: str = "raw/ingest",
        interval_minutes: int = 5,
        max_retries: int = 3,
    ) -> None:
        self._tracker = tracker
        self._pipeline = pipeline
        self._raw_ingest_dir = raw_ingest_dir
        self._interval_minutes = interval_minutes
        self._max_retries = max_retries
        self._scheduler = AsyncIOScheduler()
        self._job_id = "ingest_retry"

    async def _scan_and_retry(self) -> None:
        """Scan for failed records and attempt retry."""
        try:
            records = await self._tracker.get_failed_records(
                limit=5, max_retries=self._max_retries
            )
            if not records:
                return

            logger.info("IngestRetryScheduler: found %d failed records", len(records))

            for rec in records:
                source_id = rec.get("source_id", "")
                source_type = rec.get("source_type", "")
                raw_path = rec.get("raw_path", "")

                if not raw_path or not Path(raw_path).exists():
                    logger.warning(
                        "Cannot retry %s: raw data not found at %s",
                        source_id, raw_path,
                    )
                    await self._tracker.mark_failed(
                        source_id,
                        f"raw data not found at {raw_path}",
                    )
                    continue

                try:
                    raw_data = json.loads(
                        Path(raw_path).read_text("utf-8")
                    )
                except Exception:
                    logger.exception("Cannot read raw JSON for %s", source_id)
                    await self._tracker.mark_failed(
                        source_id, f"cannot parse raw JSON at {raw_path}"
                    )
                    continue

                # Run adapter transform + pipeline
                await self._retry_one(source_id, source_type, raw_data)

        except Exception:
            logger.exception("IngestRetryScheduler scan failed")

    async def _retry_one(
        self,
        source_id: str,
        source_type: str,
        raw_data: dict[str, Any],
    ) -> None:
        """Retry a single ingest record."""
        from app.adapters.miromind import MiroMindAdapter
        from app.models import IngestRequest

        # Map source_type to adapter
        adapter_map: dict[str, Any] = {
            "miromind": MiroMindAdapter,
        }
        adapter_cls = adapter_map.get(source_type)
        if not adapter_cls:
            logger.warning("Unknown source_type '%s' for %s", source_type, source_id)
            await self._tracker.mark_failed(
                source_id, f"unknown source_type: {source_type}"
            )
            return

        try:
            adapter = adapter_cls()
            extracted = await adapter.extract(raw_data)
            source, options = await adapter.transform(extracted)

            await self._tracker.mark_processing(source_id)
            result = await self._pipeline.run(
                IngestRequest(source=source, options=options)
            )

            if result.status.value == "completed":
                await self._tracker.mark_completed(
                    source_id, kb_task_id=result.task_id
                )
                logger.info("Retry succeeded for %s", source_id)
            else:
                await self._tracker.mark_failed(
                    source_id, result.error or "unknown error"
                )
        except Exception as exc:
            logger.exception("Retry failed for %s", source_id)
            await self._tracker.mark_failed(source_id, str(exc))

    def start(self) -> None:
        """Start the scheduler."""
        self._scheduler.add_job(
            self._scan_and_retry,
            IntervalTrigger(minutes=self._interval_minutes),
            id=self._job_id,
            replace_existing=True,
            max_instances=1,
        )
        self._scheduler.start()
        logger.info(
            "IngestRetryScheduler started (interval=%dmin, max_retries=%d)",
            self._interval_minutes,
            self._max_retries,
        )

    def shutdown(self) -> None:
        """Shutdown the scheduler."""
        self._scheduler.shutdown(wait=False)
        logger.info("IngestRetryScheduler shut down")
