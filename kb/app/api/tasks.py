"""API route: GET /api/tasks/{task_id} - task status endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.models import TaskStatus

router = APIRouter(prefix="/api", tags=["tasks"])

_pipeline = None


def set_pipeline(pipeline) -> None:
    global _pipeline
    _pipeline = pipeline


@router.get("/tasks/{task_id}", response_model=TaskStatus | None)
async def get_task_status(task_id: str) -> TaskStatus | None:
    """Get the status of a background ingest task."""
    if _pipeline is None:
        return None
    return _pipeline.get_task_status(task_id)
