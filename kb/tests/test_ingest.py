"""Tests for Ingest pipeline: preprocess, analyze, graph_process, render."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.ingest.preprocess import Preprocessor
from app.ingest.analyze import Analyzer
from app.ingest.graph_process import GraphProcessor
from app.ingest.render import MarkdownRenderer
from app.models import InputFormat, MaterialType, OCRResult

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

    @pytest.mark.asyncio
    async def test_extract_pdf_ocr_fallback(self):
        """Scanned PDF: text layer empty → OCR fallback should extract text."""
        from unittest.mock import AsyncMock

        # Mock fitz: pages return empty text, but pixmap renders images
        mock_page = MagicMock()
        mock_page.get_text.return_value = ""  # empty text layer (scanned PDF)
        mock_pix = MagicMock()
        mock_pix.tobytes.return_value = b"\x89PNG\r\n\x1a\n"
        mock_page.get_pixmap.return_value = mock_pix

        mock_doc = MagicMock()
        # Use side_effect so each iteration gets a fresh iterator
        mock_doc.__iter__ = MagicMock(side_effect=lambda: iter([mock_page, mock_page]))
        mock_doc.__len__ = MagicMock(return_value=2)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        # Mock ImageOCRExtractor.extract_batch to return list of OCR results
        mock_ocr_result = OCRResult(
            text="这是通过OCR技术从PDF扫描件中提取的文字内容。该PDF的文本层为空，系统自动启用了OCR回退机制。",
            engine="paddle",
            confidence=0.9,
            duration_ms=100.0,
        )

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            with patch("app.ingest.ocr.ImageOCRExtractor") as MockOCR:
                mock_ocr_instance = MagicMock()
                # extract_batch returns a list of OCRResult
                mock_ocr_instance.extract_batch = AsyncMock(
                    return_value=[mock_ocr_result, mock_ocr_result]
                )
                MockOCR.return_value = mock_ocr_instance

                content, title = await self.preprocessor._extract_pdf(
                    b"fake-pdf-bytes", "scanned-document.pdf"
                )

        assert "OCR" in content
        assert len(content.strip()) >= 50

    @pytest.mark.asyncio
    async def test_extract_pdf_ocr_also_fails(self):
        """Both text layer and OCR fail → should raise ValueError with friendly message."""
        from unittest.mock import AsyncMock

        mock_page = MagicMock()
        mock_page.get_text.return_value = ""  # empty text
        mock_pix = MagicMock()
        mock_pix.tobytes.return_value = b"\x89PNG\r\n\x1a\n"
        mock_page.get_pixmap.return_value = mock_pix

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(side_effect=lambda: iter([mock_page]))
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        # Mock OCR returning empty text
        mock_ocr_result = OCRResult(
            text="", engine="none", confidence=0.0, duration_ms=50.0,
        )

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            with patch("app.ingest.ocr.ImageOCRExtractor") as MockOCR:
                mock_ocr_instance = MagicMock()
                mock_ocr_instance.extract_batch = AsyncMock(
                    return_value=[mock_ocr_result]
                )
                MockOCR.return_value = mock_ocr_instance

                with pytest.raises(ValueError, match="OCR 未识别到有效文字"):
                    await self.preprocessor._extract_pdf(
                        b"fake-pdf-bytes", "encrypted-document.pdf"
                    )


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
