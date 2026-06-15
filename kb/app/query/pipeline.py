"""Query pipeline: orchestrates the 3-step knowledge query process."""

from __future__ import annotations

import logging

from app.config import Settings
from app.database import Neo4jDatabase
from app.llm import LLMClient, get_llm_client
from app.models import QueryRequest, QueryResult
from app.query.generate import AnswerGenerator
from app.query.retrieve import GraphRetriever
from app.query.understand import QueryUnderstander

logger = logging.getLogger(__name__)


class QueryPipeline:
    """Orchestrates the full 3-step query pipeline."""

    def __init__(self, settings: Settings, db: Neo4jDatabase | None = None) -> None:
        self._settings = settings
        self._db = db or Neo4jDatabase(settings)
        self._llm: LLMClient | None = None
        self._understander: QueryUnderstander | None = None
        self._retriever: GraphRetriever | None = None
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
        self._understander = QueryUnderstander(self._db, self._llm, model=self._settings.dashscope_model_analyze)
        self._retriever = GraphRetriever(self._db)
        self._generator = AnswerGenerator(self._llm, model=self._settings.dashscope_model_query)
        self._initialized = True
        logger.info("QueryPipeline initialized.")

    async def run(self, request: QueryRequest) -> QueryResult:
        """Execute the full 3-step query pipeline."""
        if not self._initialized:
            await self.initialize()

        try:
            # Step 1: Query Understanding
            understanding = await self._understander.understand(request.question)
            logger.info("Query understood: type=%s, depth=%s, entities=%s",
                        understanding.query_type, understanding.depth, understanding.entities)

            # Step 2: Graph Retrieval
            retrieval = await self._retriever.retrieve(understanding)

            # Step 3: LLM Answer Generation
            result = await self._generator.generate(
                question=request.question,
                retrieval=retrieval,
                query_type=understanding.query_type,
            )

            return result
        except Exception as exc:
            logger.exception("Query pipeline failed: %s", exc)
            return QueryResult(
                answer=f"查询失败: {exc}",
                confidence=0.0,
            )
