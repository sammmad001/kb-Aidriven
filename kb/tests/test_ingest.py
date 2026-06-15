"""Tests for Ingest pipeline: preprocess, analyze, graph_process, render."""

from __future__ import annotations

import os
import tempfile

import pytest

from app.config import Settings
from app.ingest.preprocess import Preprocessor
from app.ingest.analyze import Analyzer
from app.ingest.graph_process import GraphProcessor
from app.ingest.render import MarkdownRenderer
from app.models import InputFormat, MaterialType

from tests.conftest import MockLLMClient, MockNeo4jDatabase


# ======================================================================
# Preprocessor Tests
# ======================================================================

class TestPreprocessor:
    """Test Step 1: format preprocessing."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        settings = Settings(raw_dir=os.path.join(self.tmpdir, "raw"), wiki_dir=os.path.join(self.tmpdir, "wiki"))
        self.preprocessor = Preprocessor(settings)

    @pytest.mark.asyncio
    async def test_process_text(self):
        result = await self.preprocessor.process(
            source="RAG是检索增强生成技术",
            format=InputFormat.TEXT,
        )
        assert result.content == "RAG是检索增强生成技术"
        assert result.format == InputFormat.TEXT
        assert result.raw_path.endswith(".md")
        assert os.path.exists(result.raw_path)

    @pytest.mark.asyncio
    async def test_process_markdown(self):
        md = "# RAG\n\nRAG是一种技术"
        result = await self.preprocessor.process(
            source=md,
            format=InputFormat.MARKDOWN,
        )
        assert "# RAG" in result.content
        assert result.title == "RAG"

    @pytest.mark.asyncio
    async def test_process_text_word_count(self):
        result = await self.preprocessor.process(
            source="这是一段测试文本",
            format=InputFormat.TEXT,
        )
        assert result.word_count == len("这是一段测试文本")

    @pytest.mark.asyncio
    async def test_raw_file_immutable(self):
        """Raw files should be saved and never overwritten."""
        result1 = await self.preprocessor.process(
            source="第一条内容",
            format=InputFormat.TEXT,
        )
        result2 = await self.preprocessor.process(
            source="第二条内容",
            format=InputFormat.TEXT,
        )
        assert result1.raw_path != result2.raw_path
        # Both files exist
        assert os.path.exists(result1.raw_path)
        assert os.path.exists(result2.raw_path)


# ======================================================================
# Analyzer Tests
# ======================================================================

class TestAnalyzer:
    """Test Step 2: analysis and classification."""

    @pytest.mark.asyncio
    async def test_analyze_factual(self):
        llm = MockLLMClient(responses={
            "analysis": '{"type": "factual", "entities": [{"name": "RAG"}], "relations": [], "conflicts": [], "gaps": [], "compile_suggestion": ""}',
        })
        db = MockNeo4jDatabase()
        analyzer = Analyzer(llm, db)

        result = await analyzer.analyze("RAG是检索增强生成技术", "raw/test.md")
        assert result.type == MaterialType.FACTUAL
        assert len(result.entities) >= 1

    @pytest.mark.asyncio
    async def test_analyze_with_existing_entity(self):
        llm = MockLLMClient(responses={
            "analysis": '{"type": "conceptual", "entities": [{"name": "RAG"}], "relations": [], "conflicts": [], "gaps": [], "compile_suggestion": ""}',
        })
        db = MockNeo4jDatabase()
        # Pre-populate a node
        db._nodes["RAG"] = {"id": "RAG", "name": "RAG", "summary": "检索增强生成"}
        analyzer = Analyzer(llm, db)

        result = await analyzer.analyze("RAG的新进展", "raw/test.md")
        assert result.entities[0].exists is True
        assert result.entities[0].node_id == "RAG"

    @pytest.mark.asyncio
    async def test_analyze_llm_failure_fallback(self):
        """When LLM fails, should return a valid fallback AnalysisReport."""
        llm = MockLLMClient()
        llm.chat = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("LLM down"))
        # Make it async

        async def failing_chat(system, user, json_mode=False):
            raise RuntimeError("LLM down")

        llm.chat = failing_chat
        db = MockNeo4jDatabase()
        analyzer = Analyzer(llm, db)

        result = await analyzer.analyze("test content", "raw/test.md")
        assert result.type == MaterialType.FACTUAL
        assert "LLM" in result.gaps[0] or "失败" in result.gaps[0]


# ======================================================================
# GraphProcessor Tests
# ======================================================================

class TestGraphProcessor:
    """Test Step 3: graph processing."""

    @pytest.mark.asyncio
    async def test_compile_decision(self):
        llm = MockLLMClient()
        db = MockNeo4jDatabase()
        processor = GraphProcessor(llm, db)

        from app.models import AnalysisReport, EntityInfo, MaterialType
        analysis = AnalysisReport(
            type=MaterialType.FACTUAL,
            entities=[
                EntityInfo(name="RAG", exists=False),
                EntityInfo(name="LLM", exists=True, node_id="LLM"),
            ],
            relations=[],
        )
        actions = processor._compile_decision(analysis)
        assert len(actions) == 2
        assert any(a.is_new for a in actions)
        assert any(not a.is_new for a in actions)

    @pytest.mark.asyncio
    async def test_extract_summary(self):
        assert GraphProcessor._extract_summary("> 这是摘要\n正文内容") == "这是摘要"
        assert GraphProcessor._extract_summary("正文内容很长的内容") == "正文内容很长的内容"


# ======================================================================
# MarkdownRenderer Tests
# ======================================================================

class TestMarkdownRenderer:
    """Test Step 4: Markdown rendering."""

    @pytest.mark.asyncio
    async def test_render_node(self):
        db = MockNeo4jDatabase()
        db._nodes["RAG"] = {
            "id": "RAG",
            "name": "RAG",
            "content": "> RAG技术\n\nRAG是检索增强生成。",
            "summary": "检索增强生成技术",
            "source": "raw/test.md",
        }

        tmpdir = tempfile.mkdtemp()
        settings = Settings(raw_dir=tmpdir, wiki_dir=os.path.join(tmpdir, "wiki"))
        renderer = MarkdownRenderer(db, settings)

        path = await renderer.render_node("RAG")
        assert path is not None
        assert os.path.exists(path)
        with open(path) as f:
            content = f.read()
        assert "RAG" in content
        assert "检索增强生成" in content

    @pytest.mark.asyncio
    async def test_render_nonexistent_node(self):
        db = MockNeo4jDatabase()
        tmpdir = tempfile.mkdtemp()
        settings = Settings(raw_dir=tmpdir, wiki_dir=os.path.join(tmpdir, "wiki"))
        renderer = MarkdownRenderer(db, settings)

        path = await renderer.render_node("NONEXISTENT")
        assert path is None
