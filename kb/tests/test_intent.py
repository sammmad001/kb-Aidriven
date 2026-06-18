"""Unit tests for intent detection and conversation context."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.feishu.context import ConversationContext
from app.feishu.intent import IntentDetector


# ======================================================================
# Intent Detection Tests
# ======================================================================

class TestIntentDetection:
    """Test the three-layer intent detection architecture."""

    @pytest.fixture
    def detector_no_llm(self) -> IntentDetector:
        """IntentDetector without LLM (rules-only mode)."""
        return IntentDetector(llm=None)

    @pytest.mark.asyncio
    async def test_question_mark_detected_as_query(self, detector_no_llm: IntentDetector):
        """Text ending with ？ should be classified as query."""
        result = await detector_no_llm.detect("RAG是什么？")
        assert result == "query"

    @pytest.mark.asyncio
    async def test_english_question_mark_as_query(self, detector_no_llm: IntentDetector):
        """Text ending with ? should be classified as query."""
        result = await detector_no_llm.detect("What is RAG?")
        assert result == "query"

    @pytest.mark.asyncio
    async def test_chinese_interrogative_as_query(self, detector_no_llm: IntentDetector):
        """Chinese interrogative words should trigger query classification."""
        result = await detector_no_llm.detect("什么是知识图谱")
        assert result == "query"

    @pytest.mark.asyncio
    async def test_how_to_as_query(self, detector_no_llm: IntentDetector):
        """'如何' prefix should be classified as query."""
        result = await detector_no_llm.detect("如何使用知识图谱")
        assert result == "query"

    @pytest.mark.asyncio
    async def test_why_as_query(self, detector_no_llm: IntentDetector):
        """'为什么' prefix should be classified as query."""
        result = await detector_no_llm.detect("为什么选择Graph-First架构")
        assert result == "query"

    @pytest.mark.asyncio
    async def test_long_text_as_input(self, detector_no_llm: IntentDetector):
        """Text longer than 100 characters should be classified as input."""
        long_text = "今天学习了知识图谱的相关内容。" * 20  # ~400 chars
        result = await detector_no_llm.detect(long_text)
        assert result == "input"

    @pytest.mark.asyncio
    async def test_multi_paragraph_as_input(self, detector_no_llm: IntentDetector):
        """Multi-paragraph text (contains newlines) should be classified as input."""
        text = "第一段内容\n第二段内容\n第三段内容"
        result = await detector_no_llm.detect(text)
        assert result == "input"

    @pytest.mark.asyncio
    async def test_url_as_input(self, detector_no_llm: IntentDetector):
        """Text containing a URL should be classified as input."""
        result = await detector_no_llm.detect("https://example.com/article")
        assert result == "input"

    @pytest.mark.asyncio
    async def test_short_statement_as_input(self, detector_no_llm: IntentDetector):
        """Short declarative text without query signals should fall to LLM or default to input."""
        # Without LLM, ambiguous text defaults to input
        result = await detector_no_llm.detect("RAG是一个很好的技术")
        assert result == "input"

    @pytest.mark.asyncio
    async def test_followup_reference_with_context_as_query(self, detector_no_llm: IntentDetector):
        """Follow-up reference words should be query when user has active context."""
        result = await detector_no_llm.detect("它的优点呢", has_recent_query=True)
        assert result == "query"

    @pytest.mark.asyncio
    async def test_followup_reference_without_context_as_input(self, detector_no_llm: IntentDetector):
        """Follow-up reference words should NOT be query without active context (safe default)."""
        result = await detector_no_llm.detect("它的优点呢", has_recent_query=False)
        assert result == "input"

    @pytest.mark.asyncio
    async def test_empty_text_as_input(self, detector_no_llm: IntentDetector):
        """Empty or whitespace text should be classified as input."""
        assert await detector_no_llm.detect("") == "input"
        assert await detector_no_llm.detect("   ") == "input"

    # ------------------------------------------------------------------
    # Research intent detection tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_research_keyword_1(self, detector_no_llm: IntentDetector):
        """'深度研究' should be classified as research."""
        result = await detector_no_llm.detect("帮我深度研究量子计算最新进展")
        assert result == "research"

    @pytest.mark.asyncio
    async def test_research_keyword_2(self, detector_no_llm: IntentDetector):
        """'深入研究' should be classified as research."""
        result = await detector_no_llm.detect("深入研究RAG技术的发展趋势")
        assert result == "research"

    @pytest.mark.asyncio
    async def test_research_keyword_3(self, detector_no_llm: IntentDetector):
        """'深度分析' should be classified as research."""
        result = await detector_no_llm.detect("深度分析一下向量数据库的竞争格局")
        assert result == "research"

    @pytest.mark.asyncio
    async def test_research_overrides_question_mark(self, detector_no_llm: IntentDetector):
        """Research keyword should take priority over question mark."""
        result = await detector_no_llm.detect("帮我研究一下量子计算？")
        assert result == "research"

    @pytest.mark.asyncio
    async def test_normal_query_still_works(self, detector_no_llm: IntentDetector):
        """Normal query without research keywords should still be classified as query."""
        result = await detector_no_llm.detect("什么是知识图谱？")
        assert result == "query"

    @pytest.mark.asyncio
    async def test_llm_timeout_falls_back_to_input(self):
        """When LLM times out, should fall back to safe default 'input'."""
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=asyncio.TimeoutError())
        detector = IntentDetector(llm=mock_llm, llm_timeout=0.01)
        result = await detector.detect("ambiguous text here")
        assert result == "input"

    @pytest.mark.asyncio
    async def test_llm_error_falls_back_to_input(self):
        """When LLM raises an error, should fall back to safe default 'input'."""
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("API error"))
        detector = IntentDetector(llm=mock_llm, llm_timeout=2.0)
        result = await detector.detect("ambiguous text here")
        assert result == "input"

    @pytest.mark.asyncio
    async def test_llm_returns_query(self):
        """When LLM returns 'QUERY', should be classified as query."""
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="QUERY")
        detector = IntentDetector(llm=mock_llm, llm_timeout=2.0)
        result = await detector.detect("some ambiguous text")
        assert result == "query"


# ======================================================================
# Conversation Context Tests
# ======================================================================

class TestConversationContext:
    """Test conversation context: entity extraction and follow-up enrichment."""

    @pytest.fixture
    def context(self) -> ConversationContext:
        return ConversationContext(max_turns=5, ttl_seconds=600)

    def test_add_turn_and_get_history(self, context: ConversationContext):
        """Adding a turn should make it available in history."""
        context.add_turn("user1", "什么是RAG", "RAG是检索增强生成")
        history = context.get_history("user1")
        assert len(history) == 1
        assert history[0].question == "什么是RAG"

    def test_has_active_context(self, context: ConversationContext):
        """has_active_context should reflect whether turns exist."""
        assert not context.has_active_context("user1")
        context.add_turn("user1", "Q", "A")
        assert context.has_active_context("user1")

    def test_clear(self, context: ConversationContext):
        """clear() should remove all turns for a user."""
        context.add_turn("user1", "Q", "A")
        context.clear("user1")
        assert not context.has_active_context("user1")

    def test_max_turns_eviction(self, context: ConversationContext):
        """Oldest turns should be evicted when exceeding max_turns."""
        for i in range(7):
            context.add_turn("user1", f"Q{i}", f"A{i}")
        history = context.get_history("user1")
        assert len(history) == 5
        # First two should be evicted
        assert history[0].question == "Q2"

    def test_user_isolation(self, context: ConversationContext):
        """Different users should have isolated contexts."""
        context.add_turn("user1", "Q1", "A1")
        context.add_turn("user2", "Q2", "A2")
        assert len(context.get_history("user1")) == 1
        assert len(context.get_history("user2")) == 1
        assert context.get_history("user1")[0].question == "Q1"

    def test_entity_extraction_from_bold(self, context: ConversationContext):
        """Entities should be extracted from **bold** markdown in answers."""
        context.add_turn(
            "user1", "什么是RAG",
            "**RAG**是检索增强生成，**向量数据库**是核心组件",
        )
        history = context.get_history("user1")
        assert "RAG" in history[0].entities
        assert "向量数据库" in history[0].entities

    def test_followup_entity_enrichment(self, context: ConversationContext):
        """Follow-up with reference word should be enriched with entity context."""
        context.add_turn(
            "user1", "什么是RAG",
            "**RAG**是检索增强生成的缩写",
        )
        enriched = context.enrich_followup("user1", "它的优点是什么")
        assert "RAG" in enriched
        assert enriched.startswith("RAG")

    def test_followup_no_reference_unchanged(self, context: ConversationContext):
        """Follow-up without reference word should not be modified."""
        context.add_turn("user1", "Q", "**RAG**")
        enriched = context.enrich_followup("user1", "向量数据库的原理")
        assert enriched == "向量数据库的原理"

    def test_followup_no_context_unchanged(self, context: ConversationContext):
        """Follow-up with no prior context should return original text."""
        enriched = context.enrich_followup("user1", "它的优点")
        assert enriched == "它的优点"

    def test_get_context_history_format(self, context: ConversationContext):
        """get_context_history should return alternating user/assistant messages."""
        context.add_turn("user1", "Q1", "A1")
        context.add_turn("user1", "Q2", "A2")
        history = context.get_context_history("user1")
        assert len(history) == 4  # 2 turns × 2 messages each
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Q1"
        assert history[1]["role"] == "assistant"
