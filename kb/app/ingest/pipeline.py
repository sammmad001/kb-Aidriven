"""Ingest pipeline: orchestrates the 4-step knowledge ingestion process."""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from app.config import Settings
from app.database import Neo4jDatabase
from app.ingest.analyze import Analyzer
from app.ingest.graph_process import GraphProcessor
from app.ingest.preprocess import Preprocessor
from app.ingest.render import MarkdownRenderer
from app.llm import LLMClient, get_llm_client
from app.models import (
    IngestOptions,
    IngestRequest,
    IngestResult,
    TaskStatus,
    TaskStatusEnum,
)

logger = logging.getLogger(__name__)


class IngestPipeline:
    """Orchestrates the full 4-step ingest pipeline."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db = Neo4jDatabase(settings)
        self._llm: LLMClient | None = None
        self._preprocessor: Preprocessor | None = None
        self._analyzer: Analyzer | None = None
        self._graph_processor: GraphProcessor | None = None
        self._renderer: MarkdownRenderer | None = None
        self._initialized = False

        # In-memory task tracking (upgrade to Redis if needed)
        self._tasks: dict[str, TaskStatus] = {}

    async def initialize(self) -> None:
        """Initialize all pipeline components."""
        if self._initialized:
            return
        await self._db.connect()
        await self._db.initialize_schema()
        self._llm = get_llm_client(self._settings)
        self._preprocessor = Preprocessor(self._settings)
        self._analyzer = Analyzer(self._llm, self._db, model=self._settings.dashscope_model_analyze)
        self._graph_processor = GraphProcessor(
            self._llm, self._db,
            compile_model=self._settings.dashscope_model_compile,
            reasoning_model=self._settings.dashscope_model_reasoning,
        )
        self._renderer = MarkdownRenderer(self._db, self._settings)
        self._initialized = True
        logger.info("IngestPipeline initialized.")

    async def shutdown(self) -> None:
        """Close database connection."""
        await self._db.close()
        self._initialized = False

    async def run(self, request: IngestRequest) -> IngestResult:
        """Execute the full 4-step ingest pipeline."""
        task_id = uuid4().hex
        result = IngestResult(task_id=task_id, status=TaskStatusEnum.PROCESSING)
        self._tasks[task_id] = TaskStatus(task_id=task_id, status=TaskStatusEnum.PROCESSING, progress="starting")
        timings: dict[str, float] = {}

        try:
            if not self._initialized:
                await self.initialize()

            # Step 1: Preprocess
            t0 = time.monotonic()
            self._tasks[task_id].progress = "preprocessing"
            preprocess_result = await self._preprocessor.process(
                source=request.source,
                format=request.options.format,
                file_name=request.options.file_name,
                file_mime=request.options.file_mime,
                tags=request.options.tags,
            )
            result.raw_path = preprocess_result.raw_path
            timings["preprocess"] = round(time.monotonic() - t0, 3)

            # Step 2: Analyze & Classify (LLM call 1)
            t0 = time.monotonic()
            self._tasks[task_id].progress = "analyzing"
            analysis = await self._analyzer.analyze(
                raw_content=preprocess_result.content,
                raw_path=preprocess_result.raw_path,
            )
            result.analysis = analysis
            timings["analyze"] = round(time.monotonic() - t0, 3)

            # Step 3: Graph Processing (fast path + background LLM)
            t0 = time.monotonic()
            self._tasks[task_id].progress = "graph_processing"
            graph_result = await self._graph_processor.process(
                analysis=analysis,
                raw_content=preprocess_result.content,
                raw_path=preprocess_result.raw_path,
            )
            result.graph_result = graph_result
            timings["graph_process"] = round(time.monotonic() - t0, 3)

            # Step 4: Markdown Rendering (parallel, no LLM)
            t0 = time.monotonic()
            self._tasks[task_id].progress = "rendering"
            rendered_files = await self._renderer.render_affected(graph_result.affected_nodes)
            result.rendered_files = rendered_files
            timings["render"] = round(time.monotonic() - t0, 3)

            result.timings = timings
            result.status = TaskStatusEnum.COMPLETED
            self._tasks[task_id] = TaskStatus(
                task_id=task_id,
                status=TaskStatusEnum.COMPLETED,
                progress="completed",
                result=result,
            )
            total = sum(timings.values())
            logger.info(
                "Ingest timing: task=%s, preprocess=%.3fs, analyze=%.3fs, "
                "graph=%.3fs, render=%.3fs, total=%.3fs, entities=%d",
                task_id, timings["preprocess"], timings["analyze"],
                timings["graph_process"], timings["render"], total,
                len(graph_result.affected_nodes),
            )
            logger.info("Ingest completed: task=%s, nodes=%s, files=%s",
                        task_id, graph_result.nodes_created, rendered_files)

        except Exception as exc:
            logger.exception("Ingest failed: task=%s", task_id)
            result.status = TaskStatusEnum.FAILED
            result.error = str(exc)
            self._tasks[task_id] = TaskStatus(
                task_id=task_id,
                status=TaskStatusEnum.FAILED,
                progress="failed",
                error=str(exc),
            )

        return result

    def get_task_status(self, task_id: str) -> TaskStatus | None:
        """Get the status of a running or completed ingest task."""
        return self._tasks.get(task_id)
