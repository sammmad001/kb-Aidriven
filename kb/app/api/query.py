"""API route: POST /api/query - knowledge query endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_user_with_rate_limit
from app.models import CurrentUser, QueryRequest, QueryResult

router = APIRouter(prefix="/api", tags=["query"])

_query_pipeline = None


def set_pipeline(pipeline) -> None:
    global _query_pipeline
    _query_pipeline = pipeline


@router.post("/query", response_model=QueryResult)
async def create_query(
    request: QueryRequest,
    current_user: CurrentUser = Depends(get_current_user_with_rate_limit),
) -> QueryResult:
    """Execute a knowledge query (synchronous).

    For depth=0 (factual), returns in < 10ms.
    For higher depths, may take 1-5s.
    """
    if _query_pipeline is None:
        raise HTTPException(status_code=503, detail="Query pipeline not initialized")

    return await _query_pipeline.run(request)
