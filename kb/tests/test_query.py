"""Tests for Query pipeline: understand, retrieve, generate."""

from __future__ import annotations

import pytest

from app.models import QueryType, QueryUnderstanding
from app.query.understand import QueryUnderstander, QUERY_SIGNALS
from app.query.generate import AnswerGenerator
from app.models import RetrievalResult, QueryRequest

from tests.conftest import MockLLMClient, MockNeo4jDatabase


# ======================================================================
# Query Understanding Tests
# ======================================================================

class TestQueryUnderstanding:
    """Test Step 1: query classification and entity extraction."""

    def test_classify_factual(self):
        """Factual queries should be classified as depth=0."""
        u = QueryUnderstander.__new__(QueryUnderstander)
        result = u._classify_by_rules("RAG是什么")
        assert result["type"] == "factual"
        assert result["depth"] == 0

    def test_classify_relational(self):
        """Relational queries should be depth=1."""
        u = QueryUnderstander.__new__(QueryUnderstander)
        for q in ["RAG和知识图谱的关系", "RAG vs GraphRAG", "对比LLM Wiki和GraphRAG"]:
            result = u._classify_by_rules(q)
            assert result["type"] in ("relational", "reasoning"), f"Failed for: {q}"

    def test_classify_reasoning(self):
        """Reasoning queries should be depth=2."""
        u = QueryUnderstander.__new__(QueryUnderstander)
        result = u._classify_by_rules("为什么选择Graph-First架构")
        assert result["type"] == "reasoning"
        assert result["depth"] == 2

    def test_classify_global(self):
        """Global queries should be depth=3."""
        u = QueryUnderstander.__new__(QueryUnderstander)
        result = u._classify_by_rules("总结所有知识库方案的趋势")
        assert result["type"] == "global"
        assert result["depth"] == 3

    def test_classify_default_factual(self):
        """Unknown queries default to factual."""
        u = QueryUnderstander.__new__(QueryUnderstander)
        result = u._classify_by_rules("今天天气怎么样")
        assert result["type"] == "factual"
        assert result["depth"] == 0

    def test_priority_global_over_others(self):
        """Global keywords have highest priority."""
        u = QueryUnderstander.__new__(QueryUnderstander)
        result = u._classify_by_rules("总结RAG和知识图谱的关系")
        assert result["type"] == "global"

    def test_keyword_signals_coverage(self):
        """All signal categories should have keywords."""
        for category in ["global", "reasoning", "relational", "factual"]:
            assert len(QUERY_SIGNALS[category]) > 0


# ======================================================================
# Answer Generator Tests
# ======================================================================

class TestAnswerGenerator:
    """Test Step 3: answer generation."""

    @pytest.mark.asyncio
    async def test_factual_direct_return(self):
        """Factual queries with content should return directly without LLM."""
        llm = MockLLMClient()
        gen = AnswerGenerator(llm)

        retrieval = RetrievalResult(
            nodes=[{
                "id": "RAG",
                "name": "RAG",
                "content": "> RAG\n\nRAG是检索增强生成技术",
            }],
        )
        result = await gen.generate(
            question="RAG是什么",
            retrieval=retrieval,
            query_type=QueryType.FACTUAL,
        )
        assert "RAG" in result.answer
        assert result.confidence == 1.0
        assert len(result.sources) == 1
        assert llm.call_count == 0  # No LLM call for factual with content

    @pytest.mark.asyncio
    async def test_relational_calls_llm(self):
        """Relational queries should call LLM for answer generation."""
        llm = MockLLMClient()
        gen = AnswerGenerator(llm)

        retrieval = RetrievalResult(
            nodes=[
                {"id": "RAG", "name": "RAG", "content": "RAG技术"},
                {"id": "GraphRAG", "name": "GraphRAG", "content": "GraphRAG技术"},
            ],
            explicit_paths=[
                {"from_name": "RAG", "rel_type": "evolves_to", "to_name": "GraphRAG"},
            ],
        )
        result = await gen.generate(
            question="RAG和GraphRAG的关系",
            retrieval=retrieval,
            query_type=QueryType.RELATIONAL,
        )
        assert llm.call_count == 1
        assert result.query_type == QueryType.RELATIONAL

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self):
        """When LLM fails, should return a fallback answer."""
        llm = MockLLMClient()

        async def failing_chat(system, user, json_mode=False):
            raise RuntimeError("LLM down")

        llm.chat = failing_chat
        gen = AnswerGenerator(llm)

        retrieval = RetrievalResult(
            nodes=[{"id": "RAG", "name": "RAG", "summary": "检索增强生成"}],
        )
        result = await gen.generate(
            question="RAG是什么",
            retrieval=retrieval,
            query_type=QueryType.RELATIONAL,
        )
        assert result.confidence == 0.3
        assert "检索增强生成" in result.answer or "RAG" in result.answer

    def test_confidence_estimation(self):
        """Test confidence scoring logic."""
        # Empty retrieval → low confidence
        assert AnswerGenerator._estimate_confidence(RetrievalResult()) == 0.1

        # With nodes but nothing else → medium
        r = RetrievalResult(nodes=[{"id": "A"}])
        conf = AnswerGenerator._estimate_confidence(r)
        assert 0.4 <= conf <= 0.7

        # With everything → higher
        r = RetrievalResult(
            nodes=[{"id": "A"}, {"id": "B"}],
            explicit_paths=[{"from": "A", "to": "B"}],
            implicit_relations=[{"confidence": 0.9}],
        )
        conf = AnswerGenerator._estimate_confidence(r)
        assert conf > 0.7
