"""API route: POST /api/query - knowledge query endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import verify_api_token
from app.models import QueryRequest, QueryResult

router = APIRouter(prefix="/api", tags=["query"])

_query_pipeline = None


def set_pipeline(pipeline) -> None:
    global _query_pipeline
    _query_pipeline = pipeline


@router.post("/query", response_model=QueryResult, dependencies=[Depends(verify_api_token)])
async def create_query(request: QueryRequest) -> QueryResult:
    """Execute a knowledge query (synchronous).

    For depth=0 (factual), returns in < 10ms.
    For higher depths, may take 1-5s.
    """
    if _query_pipeline is None:
        return QueryResult(
            answer="Query pipeline not initialized",
            confidence=0.0,
        )

    return await _query_pipeline.run(request)
