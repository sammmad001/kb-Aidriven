"""Knowledge Base API - FastAPI application entry point."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import graph, ingest, ingest_adapters, query, tasks
from app.config import get_settings
from app.feishu import router as feishu_router
from app.feishu.client import FeishuClient
from app.feishu.handlers import init_handlers
from app.feishu.ws_client import FeishuWsClient
from app.ingest.pipeline import IngestPipeline
from app.lint.checker import LintChecker
from app.maintenance.implicit_reviewer import ImplicitRelationReviewer
from app.query.pipeline import QueryPipeline
from app.services.ingest_tracker import IngestTracker
from app.services.scheduler import IngestRetryScheduler


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """JSON log formatter for structured output."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Add request trace_id if available
        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id
        return json.dumps(log_entry, ensure_ascii=False)


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
# Replace formatters on all handlers
for _handler in logging.root.handlers:
    _handler.setFormatter(JsonFormatter())

logger = logging.getLogger(__name__)

# Global instances
_ingest_pipeline: IngestPipeline | None = None
_query_pipeline: QueryPipeline | None = None
_feishu_client: FeishuClient | None = None
_feishu_ws_client: FeishuWsClient | None = None
_ingest_tracker: IngestTracker | None = None
_ingest_retry_scheduler: IngestRetryScheduler | None = None
_implicit_reviewer: ImplicitRelationReviewer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup and shutdown."""
    global _ingest_pipeline, _query_pipeline, _feishu_client, _feishu_ws_client
    global _ingest_tracker, _ingest_retry_scheduler, _implicit_reviewer
    settings = get_settings()

    # Validate production configuration
    if settings.environment == "production":
        errors, warnings = settings.validate_production_config()
        for w in warnings:
            logger.warning("Production config warning: %s", w)
        if errors:
            for e in errors:
                logger.error("Production config error: %s", e)
            raise RuntimeError(
                f"Production config validation failed: {'; '.join(errors)}"
            )
        logger.info("Production config validation passed")
    else:
        logger.info("Running in dev mode — skipping production config validation")

    # Initialize ingest pipeline
    _ingest_pipeline = IngestPipeline(settings)
    await _ingest_pipeline.initialize()

    # Initialize query pipeline (shares the same DB connection)
    _query_pipeline = QueryPipeline(settings, db=_ingest_pipeline._db)
    await _query_pipeline.initialize()

    # Initialize Feishu client
    _feishu_client = FeishuClient(settings)

    # Initialize Feishu event receiver (WebSocket or Webhook mode)
    if settings.feishu_use_ws:
        _feishu_ws_client = FeishuWsClient(settings)
        await _feishu_ws_client.start()
        logger.info("Feishu event mode: WebSocket (long connection)")
    else:
        logger.info("Feishu event mode: Webhook (HTTP push) -> POST /webhook/feishu")

    # Initialize shared handlers (used by both webhook and WebSocket)
    init_handlers(_ingest_pipeline, _feishu_client, _query_pipeline)

    # Initialize lint checker
    lint_checker = LintChecker(_ingest_pipeline._db)

    # Initialize ingest tracker (Neo4j-based)
    _ingest_tracker = IngestTracker(_ingest_pipeline._db)

    # Initialize ingest retry scheduler
    _ingest_retry_scheduler = IngestRetryScheduler(
        tracker=_ingest_tracker,
        pipeline=_ingest_pipeline,
        raw_ingest_dir=settings.raw_ingest_dir,
        interval_minutes=settings.ingest_retry_interval_minutes,
    )
    if settings.ingest_auto_retry:
        _ingest_retry_scheduler.start()

    # V1.1: Initialize ImplicitRelationReviewer for periodic low-confidence edge re-evaluation
    _implicit_reviewer = ImplicitRelationReviewer(
        db=_ingest_pipeline._db,
        llm=_ingest_pipeline._llm,
        reasoning_model=settings.dashscope_model_reasoning,
        interval_hours=24,
    )
    _implicit_reviewer.start()

    # Wire up API routes
    ingest.set_pipeline(_ingest_pipeline)
    query.set_pipeline(_query_pipeline)
    tasks.set_pipeline(_ingest_pipeline)
    graph.set_db(_ingest_pipeline._db, lint_checker)
    ingest_adapters.set_pipeline(_ingest_pipeline)
    ingest_adapters.set_tracker(_ingest_tracker)
    ingest_adapters.set_raw_ingest_dir(settings.raw_ingest_dir)

    logger.info("Knowledge Base API started successfully")
    yield

    # Shutdown
    if _implicit_reviewer:
        _implicit_reviewer.shutdown()
    if _ingest_retry_scheduler:
        _ingest_retry_scheduler.shutdown()
    if _feishu_ws_client:
        await _feishu_ws_client.stop()
    # Close httpx clients to prevent resource leaks (RES-02/RES-03 fix)
    if _feishu_client:
        await _feishu_client.close()
    if _ingest_pipeline:
        if _ingest_pipeline._llm:
            await _ingest_pipeline._llm.close()
        if _ingest_pipeline._preprocessor:
            await _ingest_pipeline._preprocessor.close()
        await _ingest_pipeline.shutdown()
    logger.info("Knowledge Base API shut down")


app = FastAPI(
    title="Knowledge Base API",
    description="Graph-First 个人知识库系统 API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
_cors_origins = get_settings().cors_origins
_origins_list = [o.strip() for o in _cors_origins.split(",")] if _cors_origins != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(ingest.router)
app.include_router(ingest_adapters.router)
app.include_router(query.router)
app.include_router(tasks.router)
app.include_router(graph.router)
app.include_router(feishu_router.router)


# ---------------------------------------------------------------------------
# Request tracing middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def add_trace_id(request: Request, call_next):
    """Add trace_id to each request for log correlation."""
    import uuid
    trace_id = uuid.uuid4().hex[:8]
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info(
        "%s %s -> %d (%.1fms) [trace=%s]",
        request.method, request.url.path, response.status_code, elapsed_ms, trace_id,
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


# ---------------------------------------------------------------------------
# Enhanced health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Deep health check: verifies Neo4j, LLM config, and Feishu WS status."""
    components: dict[str, str] = {}

    # Check Neo4j
    try:
        if _ingest_pipeline and _ingest_pipeline._db:
            records = await _ingest_pipeline._db.execute_read("RETURN 1 AS ok")
            components["neo4j"] = "ok" if records else "degraded"
        else:
            components["neo4j"] = "not_initialized"
    except Exception as exc:
        components["neo4j"] = f"error: {exc}"

    # Check LLM config
    settings = get_settings()
    if settings.dashscope_api_key:
        components["llm"] = f"ok ({settings.llm_provider}/{settings.dashscope_model})"
    else:
        components["llm"] = "not_configured"

    # Check Feishu event receiver
    if settings.feishu_use_ws:
        # WebSocket mode: check connection status
        if _feishu_ws_client and _feishu_ws_client.is_connected:
            components["feishu"] = "ws:connected"
        elif _feishu_ws_client and _feishu_ws_client._running:
            components["feishu"] = "ws:reconnecting"
        elif _feishu_ws_client:
            components["feishu"] = "ws:disconnected"
        else:
            components["feishu"] = "ws:not_configured"
    else:
        # Webhook mode: endpoint is always ready if server is running
        components["feishu"] = "webhook:/webhook/feishu"

    # Overall status
    all_ok = all(
        v.startswith("ok") or v.startswith("ws:connected") or v.startswith("webhook:")
        for v in components.values()
    )

    # Ingest automation status
    if _ingest_retry_scheduler:
        components["ingest_scheduler"] = "ok"
    else:
        components["ingest_scheduler"] = "not_configured"

    if _implicit_reviewer and _implicit_reviewer._running:
        components["implicit_reviewer"] = "ok"
    else:
        components["implicit_reviewer"] = "not_configured"

    status = "ok" if all_ok else "degraded"

    return {
        "status": status,
        "service": "knowledge-base-api",
        "version": "1.0.0",
        "components": components,
    }


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
