"""Query pipeline: orchestrates the multi-step knowledge query process.

V1.2: Refactored with strategy-chain architecture:
  Step 1: Query Understanding (rule + LLM classification)
  Step 2: Entity Resolution (extract strings → resolve to Neo4j nodes)
  Step 3: Graph Retrieval (adaptive depth + multi-tier fallback)
  Step 4: Quality Gate (assess sufficiency before LLM call)
  Step 5: Answer Generation (LLM synthesis with context)

Includes PipelineTrace for structured observability across all steps.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.database import Neo4jDatabase
from app.llm import LLMClient, get_llm_client
from app.models import EntityResolution, QueryRequest, QueryResult, QueryStatus, QueryUnderstanding
from app.query.gate import GateDecision, QualityGate
from app.query.generate import AnswerGenerator
from app.query.resolve import EntityResolver
from app.query.retrieve import GraphRetriever
from app.query.trace import PipelineTrace
from app.query.understand import QueryUnderstander

logger = logging.getLogger(__name__)


class QueryPipeline:
    """Orchestrates the full multi-step query pipeline with quality gating."""

    def __init__(self, settings: Settings, db: Neo4jDatabase | None = None) -> None:
        self._settings = settings
        self._db = db or Neo4jDatabase(settings)
        self._llm: LLMClient | None = None
        self._understander: QueryUnderstander | None = None
        self._resolver: EntityResolver | None = None
        self._retriever: GraphRetriever | None = None
        self._gate: QualityGate | None = None
        self._generator: AnswerGenerator | None = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Public accessors (avoid direct private attribute access from outside)
    # ------------------------------------------------------------------

    @property
    def db(self) -> Neo4jDatabase:
        """The underlying Neo4j database instance."""
        return self._db

    @property
    def llm(self) -> LLMClient | None:
        """The LLM client, or None before initialize()."""
        return self._llm

    async def initialize(self) -> None:
        """Initialize all pipeline components."""
        if self._initialized:
            return
        if not self._db.is_connected:
            await self._db.connect()
        self._llm = get_llm_client(self._settings)
        self._understander = QueryUnderstander(self._db, self._llm, model=self._settings.deepseek_model_analyze)
        self._resolver = EntityResolver(self._db)
        self._retriever = GraphRetriever(self._db)
        self._gate = QualityGate(min_nodes=1)
        self._generator = AnswerGenerator(self._llm, model=self._settings.deepseek_model_query)
        self._initialized = True
        logger.info("QueryPipeline initialized (V1.2 with EntityResolver + QualityGate + PipelineTrace).")

    async def run(self, request: QueryRequest) -> QueryResult:
        """Execute the full query pipeline with tracing and quality gating."""
        if not self._initialized:
            await self.initialize()

        trace = PipelineTrace()

        try:
            # ----------------------------------------------------------------
            # Step 1: Query Understanding
            # ----------------------------------------------------------------
            trace.step_start("understand", question=request.question[:80])
            understanding = await self._understander.understand(request.question)
            trace.step_end(
                output_summary=f"type={understanding.query_type.value}, "
                               f"depth={understanding.depth}, "
                               f"entities={len(understanding.entities)}"
            )

            # ----------------------------------------------------------------
            # Step 2: Entity Resolution (NEW V1.2)
            # ----------------------------------------------------------------
            trace.step_start("resolve", candidates=f"{understanding.entities}"[:120])
            resolution = await self._resolver.resolve(understanding.entities)
            trace.step_end(
                output_summary=f"resolved={len(resolution.resolved)}, "
                               f"unresolved={len(resolution.unresolved)}"
            )

            # Use resolved entities for retrieval; fall back to original if none resolved
            effective_entities = resolution.resolved if resolution.resolved else understanding.entities

            # ----------------------------------------------------------------
            # Step 3: Graph Retrieval
            # ----------------------------------------------------------------
            # Create a modified understanding with resolved entities
            from app.models import QueryUnderstanding
            resolved_understanding = QueryUnderstanding(
                query_type=understanding.query_type,
                entities=effective_entities,
                depth=understanding.depth,
                keywords=understanding.keywords,
            )

            trace.step_start("retrieve", entities=f"{effective_entities}"[:120])
            retrieval = await self._retriever.retrieve(resolved_understanding)
            trace.step_end(
                output_summary=f"nodes={len(retrieval.nodes)}, "
                               f"paths={len(retrieval.explicit_paths)}, "
                               f"implicit={len(retrieval.implicit_relations)}"
            )

            # ----------------------------------------------------------------
            # Step 4: Quality Gate (V1.2)
            # ----------------------------------------------------------------
            trace.step_start("gate")
            decision = self._gate.assess(retrieval, resolution)
            trace.step_end(output_summary=decision.value)

            if decision == GateDecision.INSUFFICIENT:
                trace.log()
                return QueryResult(
                    answer=self._build_insufficient_response(request.question, understanding, resolution),
                    status=QueryStatus.INSUFFICIENT,
                    unresolved_entities=resolution.unresolved,
                    search_suggestions=resolution.suggestions,
                    confidence=0.0,
                    query_type=understanding.query_type,
                    depth=0,
                    trace_id=trace.trace_id,
                )

            # ----------------------------------------------------------------
            # Step 5: LLM Answer Generation
            # ----------------------------------------------------------------
            trace.step_start("generate")
            result = await self._generator.generate(
                question=request.question,
                retrieval=retrieval,
                query_type=understanding.query_type,
                context_history=request.context_history,
            )

            # Enrich result with trace data
            result.trace_id = trace.trace_id
            if decision == GateDecision.PARTIAL:
                result.status = QueryStatus.PARTIAL
                result.unresolved_entities = resolution.unresolved
                result.search_suggestions = resolution.suggestions

            trace.step_end(
                output_summary=f"answer_len={len(result.answer)}, "
                               f"confidence={result.confidence}, "
                               f"sources={len(result.sources)}"
            )
            trace.log()
            return result

        except Exception as exc:
            trace.step_end(error=str(exc)[:100]) if trace._current_step else None
            trace.log()
            logger.exception("Query pipeline failed for trace=%s: %s", trace.trace_id, exc)
            return QueryResult(
                answer=f"查询失败: {exc}",
                status=QueryStatus.ERROR,
                confidence=0.0,
                trace_id=trace.trace_id,
            )

    # ------------------------------------------------------------------
    # Response builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_insufficient_response(
        question: str,
        understanding: "QueryUnderstanding",
        resolution: "EntityResolution",
    ) -> str:
        """Build a helpful response when no matching knowledge is found."""
        parts = [f"未在知识库中找到与「{question}」相关的内容。\n"]
        parts.append("\n可能原因：")
        parts.append("1. 该主题尚未录入知识库")
        parts.append("2. 查询关键词与知识库节点名称不完全匹配")

        if understanding.entities:
            parts.append(f"\n解析到的实体：{'、'.join(understanding.entities)}")
        if resolution.unresolved:
            parts.append(f"未匹配的实体：{'、'.join(resolution.unresolved)}")
        if resolution.suggestions:
            parts.append("\n搜索建议：")
            for s in resolution.suggestions:
                parts.append(f"  - {s}")

        parts.append("\n提示：使用 /input 命令录入相关知识。")
        return "\n".join(parts)
