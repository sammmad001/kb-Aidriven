"""API route: POST /api/ingest - knowledge ingestion endpoint."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.api.deps import verify_api_token
from app.models import IngestRequest

router = APIRouter(prefix="/api", tags=["ingest"])

# Set by main.py at startup
_pipeline = None


def set_pipeline(pipeline) -> None:
    global _pipeline
    _pipeline = pipeline


@router.post("/ingest", status_code=202, dependencies=[Depends(verify_api_token)])
async def create_ingest(request: IngestRequest, bg: BackgroundTasks) -> dict:
    """Submit a knowledge ingestion task (async).

    Returns immediately with status. Processing happens in background.
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")

    async def _run():
        await _pipeline.run(request)

    bg.add_task(_run)
    return {"status": "queued", "message": "Ingest task submitted"}
