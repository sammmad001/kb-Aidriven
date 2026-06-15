"""API route: GET /api/tasks/{task_id} - task status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user_or_service
from app.models import CurrentUser, TaskStatus

router = APIRouter(prefix="/api", tags=["tasks"])

_pipeline = None


def set_pipeline(pipeline) -> None:
    global _pipeline
    _pipeline = pipeline


@router.get("/tasks/{task_id}", response_model=TaskStatus | None)
async def get_task_status(
    task_id: str,
    current_user: CurrentUser = Depends(get_current_user_or_service),
) -> TaskStatus | None:
    """Get the status of a background ingest task."""
    if _pipeline is None:
        return None
    return _pipeline.get_task_status(task_id)
