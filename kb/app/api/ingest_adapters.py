"""API endpoints for channel-specific knowledge ingestion adapters."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.adapters.miromind import MiroMindAdapter
from app.api.deps import verify_api_token
from app.models import IngestRequest, IngestResult
from app.services.ingest_tracker import (
    IngestTracker,
    STATUS_COMPLETED,
    STATUS_SKIPPED,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest-adapters"])

# Set by main.py at startup
_pipeline = None
_tracker: IngestTracker | None = None
_raw_ingest_dir: str = "raw/ingest"


def set_pipeline(pipeline) -> None:
    global _pipeline
    _pipeline = pipeline


def set_tracker(tracker: IngestTracker) -> None:
    global _tracker
    _tracker = tracker


def set_raw_ingest_dir(path: str) -> None:
    global _raw_ingest_dir
    _raw_ingest_dir = path


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class MiroMindMessagePayload(BaseModel):
    """Payload sent by MiroMind when a deep research session completes."""

    session_id: int
    message_id: int
    session_title: str = "MiroMind 研究"
    session_model: str = ""
    content: str = ""
    thinking_text: str = ""
    tool_events: list[dict[str, Any]] = Field(default_factory=list)
    total_tokens: int = 0
    duration_ms: int = 0
    status: str = "completed"
    model: str = ""


# ---------------------------------------------------------------------------
# MiroMind ingest endpoint
# ---------------------------------------------------------------------------


@router.post("/miromind", status_code=202, dependencies=[Depends(verify_api_token)])
async def ingest_miromind(
    payload: MiroMindMessagePayload,
    bg: BackgroundTasks,
) -> dict:
    """Receive a MiroMind research completion event and trigger KB ingestion.

    Called by MiroMind's StreamRecorder hook when an assistant message
    is persisted. Runs validation + transformation + pipeline asynchronously.
    """
    if _pipeline is None or _tracker is None:
        raise HTTPException(status_code=503, detail="Ingest service not initialized")

    adapter = MiroMindAdapter()

    # 1. Extract structured knowledge
    raw_dict = payload.model_dump()
    extracted = await adapter.extract(raw_dict)

    # 2. Validate quality thresholds
    is_valid, reason = await adapter.validate(extracted)
    if not is_valid:
        await _tracker.mark_skipped(extracted.source_id, reason)
        return {
            "status": "skipped",
            "source_id": extracted.source_id,
            "reason": reason,
        }

    # 3. Persist raw JSON for potential retry
    raw_path = ""
    if _raw_ingest_dir:
        try:
            dir_path = Path(_raw_ingest_dir) / "miromind"
            dir_path.mkdir(parents=True, exist_ok=True)
            file_path = dir_path / f"{extracted.source_id}.json"
            file_path.write_text(
                json.dumps(raw_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            raw_path = str(file_path)
        except Exception as exc:
            logger.warning("Failed to persist raw JSON for %s: %s",
                           extracted.source_id, exc)

    # 4. Record attempt (idempotent: skips if already exists)
    record = await _tracker.record_attempt(
        source_id=extracted.source_id,
        source_type=adapter.source_type,
        content_hash=extracted.content_hash,
        raw_path=raw_path,
    )
    if record["exists"] and record["status"] in (STATUS_COMPLETED, STATUS_SKIPPED):
        return {
            "status": record["status"],
            "source_id": extracted.source_id,
            "reason": "already processed",
        }
    if record["exists"] and record["status"] == "processing":
        return {
            "status": "processing",
            "source_id": extracted.source_id,
            "reason": "already in progress",
        }

    # 5. Transform to IngestRequest and run pipeline in background
    source, options = await adapter.transform(extracted)

    async def _run_ingest():
        try:
            await _tracker.mark_processing(extracted.source_id)
            result: IngestResult = await _pipeline.run(
                IngestRequest(source=source, options=options)
            )
            if result.status.value == "completed":
                await _tracker.mark_completed(
                    extracted.source_id, kb_task_id=result.task_id
                )
            else:
                await _tracker.mark_failed(
                    extracted.source_id,
                    result.error or "unknown error",
                )
        except Exception as exc:
            logger.exception("Ingest failed for %s", extracted.source_id)
            await _tracker.mark_failed(extracted.source_id, str(exc))

    bg.add_task(_run_ingest)

    return {
        "status": "processing",
        "source_id": extracted.source_id,
        "content_hash": extracted.content_hash,
    }


# ---------------------------------------------------------------------------
# Monitoring / control endpoints
# ---------------------------------------------------------------------------


@router.get("/status", dependencies=[Depends(verify_api_token)])
async def ingest_status() -> dict:
    """Get aggregate ingest statistics across all channels."""
    if _tracker is None:
        raise HTTPException(status_code=503, detail="Ingest tracker not initialized")
    stats = await _tracker.get_stats()
    return {"ok": True, "stats": stats}


@router.get("/status/{source_id}", dependencies=[Depends(verify_api_token)])
async def ingest_source_status(source_id: str) -> dict:
    """Get status of a specific ingest record by source_id."""
    if _tracker is None:
        raise HTTPException(status_code=503, detail="Ingest tracker not initialized")
    record = await _tracker.get_record(source_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"ok": True, "record": record}


@router.post("/retry", dependencies=[Depends(verify_api_token)])
async def ingest_retry_all() -> dict:
    """Trigger retry of all failed ingest records (manual override)."""
    if _tracker is None:
        raise HTTPException(status_code=503, detail="Ingest tracker not initialized")
    # The scheduler handles this; this endpoint provides a manual trigger reference
    return {"ok": True, "message": "Use the scheduler or retry individual records"}


@router.post("/retry/{source_id}", dependencies=[Depends(verify_api_token)])
async def ingest_retry_one(source_id: str, bg: BackgroundTasks) -> dict:
    """Manually retry a single failed ingest record."""
    if _pipeline is None or _tracker is None:
        raise HTTPException(status_code=503, detail="Ingest service not initialized")

    record = await _tracker.get_record(source_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # Re-read raw JSON and re-queue
    raw_path = record.get("raw_path", "")
    if raw_path:
        try:
            raw_data = json.loads(Path(raw_path).read_text("utf-8"))
        except Exception:
            raise HTTPException(
                status_code=400, detail=f"Cannot read raw data from {raw_path}"
            )
    else:
        raise HTTPException(status_code=400, detail="No raw data path for this record")

    source_type = record.get("source_type", "")
    if source_type == "miromind":
        adapter = MiroMindAdapter()
    else:
        raise HTTPException(
            status_code=400, detail=f"Unknown source type: {source_type}"
        )

    extracted = await adapter.extract(raw_data)
    source, options = await adapter.transform(extracted)

    async def _retry():
        try:
            await _tracker.mark_processing(source_id)
            result = await _pipeline.run(
                IngestRequest(source=source, options=options)
            )
            if result.status.value == "completed":
                await _tracker.mark_completed(source_id, kb_task_id=result.task_id)
            else:
                await _tracker.mark_failed(source_id, result.error or "unknown error")
        except Exception as exc:
            logger.exception("Retry failed for %s", source_id)
            await _tracker.mark_failed(source_id, str(exc))

    bg.add_task(_retry)
    return {"ok": True, "source_id": source_id, "status": "processing"}
