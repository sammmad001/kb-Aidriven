"""Tests for Feishu integration: crypto, client, cards, router."""

from __future__ import annotations

import json

import pytest

from app.feishu.cards import (
    build_ack_card,
    build_analysis_card,
    build_complete_card,
    build_error_card,
    build_factual_result_card,
    build_help_card,
    build_query_card,
    build_stats_card,
)
from app.feishu.crypto import verify_signature
from app.feishu.handlers import parse_command
from app.models import (
    AnalysisReport,
    EntityInfo,
    GraphProcessResult,
    GraphStats,
    ImplicitRelation,
    ImplicitRelationType,
    IngestResult,
    MaterialType,
    QueryResult,
    QueryType,
    RelationInfo,
    SourceReference,
    TaskStatusEnum,
)


# ======================================================================
# Crypto Tests
# ======================================================================

class TestCrypto:
    """Test signature verification."""

    def test_verify_signature_with_valid_data(self):
        """Valid signature should pass verification."""
        token = "test_verification_token"
        timestamp = "1234567890"
        nonce = "test_nonce"
        body = '{"type":"url_verification"}'

        import hashlib
        sign_base = f"{timestamp}{nonce}{token}{body}"
        computed = hashlib.sha256(sign_base.encode("utf-8")).hexdigest()

        assert verify_signature(token, timestamp, nonce, body, computed) is True

    def test_verify_signature_with_invalid_signature(self):
        """Invalid signature should fail."""
        assert verify_signature("token", "ts", "nonce", "body", "wrong_signature") is False

    def test_verify_signature_with_empty_fields(self):
        """Empty fields should fail."""
        assert verify_signature("", "ts", "nonce", "body", "sig") is False
        assert verify_signature("token", "", "nonce", "body", "sig") is False


# ======================================================================
# Cards Tests
# ======================================================================

class TestCards:
    """Test Feishu card builders."""

    def test_build_ack_card(self):
        card = build_ack_card("task-001", "text")
        assert card["header"]["title"]["content"] == "📋 已收到"
        assert "task-001" in json.dumps(card)
        assert "elements" in card

    def test_build_analysis_card(self):
        analysis = AnalysisReport(
            type=MaterialType.FACTUAL,
            entities=[EntityInfo(name="RAG", exists=False)],
            relations=[],
            conflicts=[],
            gaps=[],
        )
        card = build_analysis_card(analysis)
        card_json = json.dumps(card, ensure_ascii=False)
        assert "factual" in card_json or "relational" in card_json
        assert "RAG" in card_json

    def test_build_analysis_card_with_conflicts(self):
        from app.models import ConflictInfo, ConflictType
        analysis = AnalysisReport(
            type=MaterialType.RELATIONAL,
            entities=[],
            relations=[],
            conflicts=[ConflictInfo(node="RAG", existing="旧信息", new="新信息")],
            gaps=["缺少数据"],
        )
        card = build_analysis_card(analysis)
        card_json = json.dumps(card, ensure_ascii=False)
        assert "检测到矛盾" in card_json or "矛盾" in card_json
        assert "知识缺口" in card_json

    def test_build_complete_card(self):
        result = IngestResult(
            task_id="t1",
            status=TaskStatusEnum.COMPLETED,
            graph_result=GraphProcessResult(
                nodes_created=["RAG", "LLM"],
                nodes_updated=["Knowledge_Graph"],
                explicit_edges=["RAG-[uses]->LLM"],
            ),
            rendered_files=["wiki/RAG.md"],
        )
        card = build_complete_card(result)
        card_json = json.dumps(card, ensure_ascii=False)
        assert "知识收录完成" in card_json
        assert "新增知识点" in card_json
        assert "更新知识点" in card_json
        assert "RAG" in card_json
        assert "Knowledge Graph" in card_json or "Knowledge_Graph" in card_json

    def test_build_factual_result_card(self):
        qr = QueryResult(
            answer="RAG是检索增强生成",
            sources=[SourceReference(node_id="RAG", node_name="RAG")],
            query_type=QueryType.FACTUAL,
        )
        card = build_factual_result_card(qr)
        assert "RAG" in json.dumps(card)

    def test_build_query_card_dispatches_by_type(self):
        for qt in [QueryType.FACTUAL, QueryType.RELATIONAL, QueryType.REASONING, QueryType.GLOBAL]:
            qr = QueryResult(answer="test", query_type=qt)
            card = build_query_card(qr)
            assert "elements" in card

    def test_build_stats_card(self):
        stats = GraphStats(node_count=10, edge_count=5, cluster_count=2)
        card = build_stats_card(stats)
        assert "10" in json.dumps(card)

    def test_build_error_card(self):
        card = build_error_card("错误", "出错了", "请重试")
        card_json = json.dumps(card, ensure_ascii=False)
        assert "出错了" in card_json
        assert "重试" in card_json

    def test_build_help_card(self):
        card = build_help_card()
        assert "/q" in json.dumps(card)
        assert "/stats" in json.dumps(card)
        assert "/help" in json.dumps(card)


# ======================================================================
# Router Command Parsing Tests
# ======================================================================

class TestCommandParsing:
    """Test Feishu router command parsing."""

    def test_parse_query_command(self):
        cmd, args = parse_command("/q RAG是什么")
        assert cmd == "query"
        assert args == "RAG是什么"

    def test_parse_query_long_form(self):
        cmd, args = parse_command("/query RAG和知识图谱的关系")
        assert cmd == "query"
        assert args == "RAG和知识图谱的关系"

    def test_parse_stats_command(self):
        cmd, args = parse_command("/stats")
        assert cmd == "stats"
        assert args == ""

    def test_parse_search_command(self):
        cmd, args = parse_command("/search 知识图谱")
        assert cmd == "search"
        assert args == "知识图谱"

    def test_parse_recent_command(self):
        cmd, args = parse_command("/recent")
        assert cmd == "recent"

    def test_parse_help_command(self):
        cmd, args = parse_command("/help")
        assert cmd == "help"

    def test_parse_plain_text_as_input(self):
        cmd, args = parse_command("这是一段关于RAG的笔记")
        assert cmd == "input"
        assert args == "这是一段关于RAG的笔记"

    def test_parse_case_insensitive(self):
        cmd, args = parse_command("/Q test query")
        assert cmd == "query"
        assert args == "test query"

    def test_parse_help_case_insensitive(self):
        cmd, args = parse_command("/Help")
        assert cmd == "help"


# ======================================================================
# Entity Filtering & Performance Tests
# ======================================================================

class TestEntityFiltering:
    """Test entity post-filtering and quality control."""

    def test_filter_entities_removes_generic_phrases(self):
        from app.ingest.analyze import Analyzer
        # Create analyzer with dummy deps
        analyzer = Analyzer.__new__(Analyzer)
        entities = [
            EntityInfo(name="RAG架构"),
            EntityInfo(name="创新高"),
            EntityInfo(name="杀估值"),
            EntityInfo(name="Neo4j"),
        ]
        raw_result = {"entities": [
            {"name": "RAG架构", "importance": 9},
            {"name": "创新高", "importance": 3},
            {"name": "杀估值", "importance": 2},
            {"name": "Neo4j", "importance": 8},
        ]}
        filtered = analyzer._filter_entities(entities, raw_result)
        names = [e.name for e in filtered]
        assert "RAG架构" in names
        assert "Neo4j" in names
        assert "创新高" not in names
        assert "杀估值" not in names

    def test_filter_entities_cap_at_10(self):
        from app.ingest.analyze import Analyzer
        analyzer = Analyzer.__new__(Analyzer)
        entities = [EntityInfo(name=f"Entity{i}") for i in range(20)]
        raw_result = {"entities": [
            {"name": f"Entity{i}", "importance": 20 - i} for i in range(20)
        ]}
        filtered = analyzer._filter_entities(entities, raw_result)
        assert len(filtered) == 10
        # Top importance should come first
        assert filtered[0].name == "Entity0"

    def test_filter_entities_removes_short_names(self):
        from app.ingest.analyze import Analyzer
        analyzer = Analyzer.__new__(Analyzer)
        entities = [
            EntityInfo(name="A"),
            EntityInfo(name="RAG"),
        ]
        raw_result = {"entities": [
            {"name": "A", "importance": 10},
            {"name": "RAG", "importance": 5},
        ]}
        filtered = analyzer._filter_entities(entities, raw_result)
        assert len(filtered) == 1
        assert filtered[0].name == "RAG"


class TestSimpleContentGeneration:
    """Test fast-path content generation without LLM."""

    def test_generate_simple_content(self):
        from app.ingest.graph_process import GraphProcessor
        processor = GraphProcessor.__new__(GraphProcessor)
        analysis = AnalysisReport(
            type=MaterialType.CONCEPTUAL,
            entities=[EntityInfo(name="RAG", exists=False)],
            relations=[RelationInfo(from_entity="RAG", to_entity="LLM", type="uses")],
            conflicts=[],
            gaps=[],
            compile_suggestion="create concept pages",
        )
        raw = "RAG (Retrieval Augmented Generation) is a technique that combines retrieval with generation.\n\nLLM stands for Large Language Model."
        content = processor._generate_simple_content("RAG", "Concept", raw, analysis)
        assert "RAG" in content
        assert "Concept" in content
        assert "LLM" in content

    def test_extract_relevant_paragraphs(self):
        from app.ingest.graph_process import GraphProcessor
        raw = "RAG is a technique.\n\nLLM is a language model.\n\nRAG uses retrieval."
        result = GraphProcessor._extract_relevant_paragraphs("RAG", raw, max_chars=800)
        assert "RAG" in result
        # Should not include the LLM-only paragraph if RAG paragraphs are found
        paragraphs = result.split("\n\n")
        for p in paragraphs:
            assert "RAG" in p or len(paragraphs) == 1

    def test_extract_relevant_paragraphs_fallback(self):
        from app.ingest.graph_process import GraphProcessor
        raw = "Nothing about the entity here.\n\nAnother paragraph."
        result = GraphProcessor._extract_relevant_paragraphs("MissingEntity", raw, max_chars=800)
        # Should fallback to first chunk of raw content
        assert len(result) > 0
