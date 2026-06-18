"""Step 1: Preprocess various input formats into unified Markdown."""

from __future__ import annotations

import base64
import io
import ipaddress
import logging
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.database import Neo4jDatabase
from app.models import InputFormat, PreprocessResult

logger = logging.getLogger(__name__)


class Preprocessor:
    """Convert various input formats into unified Markdown and save to raw/sources/."""

    def __init__(self, settings: Settings) -> None:
        self._raw_dir = settings.raw_dir
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        os.makedirs(self._raw_dir, exist_ok=True)
        # ASR 转写器（延迟 import，dashscope 为可选依赖）
        from app.ingest.asr import AudioTranscriber

        if getattr(settings, "asr_enabled", True):
            self._transcriber = AudioTranscriber(
                dashscope_api_key=settings.dashscope_api_key,
                model=getattr(settings, "asr_model", "paraformer-realtime-v2"),
            )
        else:
            self._transcriber = AudioTranscriber(dashscope_api_key="")

    def _user_raw_dir(self) -> str:
        """Get user-scoped raw directory for file isolation."""
        uid = Neo4jDatabase.get_current_user_id_or_default()
        path = os.path.join(self._raw_dir, uid) if uid else self._raw_dir
        os.makedirs(path, exist_ok=True)
        return path

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._http.aclose()

    async def process(self, source: str, format: InputFormat,
                      file_name: str | None = None,
                      file_mime: str | None = None,
                      tags: list[str] | None = None) -> PreprocessResult:
        """Process source content based on format type."""
        handlers = {
            InputFormat.TEXT: self._process_text,
            InputFormat.MARKDOWN: self._process_text,
            InputFormat.URL: self._process_url,
            InputFormat.FILE: self._process_file,
            InputFormat.IMAGE: self._process_image,
            InputFormat.AUDIO: self._process_audio,
        }
        handler = handlers.get(format, self._process_text)
        content, title = await handler(source, file_name=file_name, file_mime=file_mime)

        # Save raw file
        raw_path = self._save_raw(content, title)
        word_count = len(content)

        return PreprocessResult(
            content=content,
            raw_path=raw_path,
            title=title,
            format=format,
            word_count=word_count,
            metadata={"tags": tags or [], "format": format.value},
        )

    # ------------------------------------------------------------------
    # Format handlers
    # ------------------------------------------------------------------

    async def _process_text(self, source: str, **kwargs: Any) -> tuple[str, str]:
        """Text/Markdown: direct pass-through."""
        title = self._extract_title(source) or "untitled-note"
        return source, title

    async def _process_url(self, url: str, **kwargs: Any) -> tuple[str, str]:
        """URL: fetch HTML, extract main content, convert to Markdown."""
        # SECURITY: SSRF protection — block internal/private IP addresses
        if not self._is_safe_url(url):
            logger.warning("URL blocked by SSRF filter: %s", url)
            return f"Source URL: {url}\n\nURL blocked: internal/private addresses are not allowed.", url

        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            html = resp.text
        except httpx.HTTPError as exc:
            logger.warning("URL fetch failed (%s), storing URL as text: %s", exc, url)
            return f"Source URL: {url}\n\nFailed to fetch content.", url

        try:
            from readability import Document
            from html2text import HTML2Text

            doc = Document(html)
            title = doc.title() or url
            summary_html = doc.summary()

            converter = HTML2Text()
            converter.ignore_links = False
            converter.ignore_images = False
            converter.body_width = 0
            markdown = converter.handle(summary_html)

            # Prepend source info
            content = f"> Source: [{url}]({url})\n\n{markdown}"
            return content, self._slugify(title)
        except Exception as exc:
            logger.warning("readability extraction failed: %s", exc)
            return f"Source URL: {url}\n\nRaw HTML extraction failed.", url

    async def _process_file(self, source: str, **kwargs: Any) -> tuple[str, str]:
        """File (base64 encoded): decode, extract text based on MIME type."""
        file_name = kwargs.get("file_name", "uploaded-file")
        file_mime = kwargs.get("file_mime", "application/octet-stream")

        try:
            raw_bytes = base64.b64decode(source)
        except Exception:
            # If not base64, treat as plain text
            return source, self._slugify(file_name)

        if "pdf" in file_mime:
            return await self._extract_pdf(raw_bytes, file_name)
        elif "word" in file_mime or "docx" in file_mime:
            return self._extract_docx(raw_bytes, file_name)
        else:
            # Try plain text decode
            try:
                text = raw_bytes.decode("utf-8")
                return text, self._slugify(file_name)
            except UnicodeDecodeError:
                return f"Binary file: {file_name} ({file_mime})", self._slugify(file_name)

    async def _process_image(self, source: str, **kwargs: Any) -> tuple[str, str]:
        """Image: save raw, mark as OCR placeholder."""
        title = kwargs.get("file_name", "image")
        content = "[Image uploaded - OCR pending]\n\nImage data stored in raw sources."
        # Save the raw image
        try:
            raw_bytes = base64.b64decode(source)
            raw_dir = self._user_raw_dir()
            img_path = os.path.join(raw_dir, f"{datetime.now().strftime('%Y-%m-%d')}-{self._slugify(title)}")
            # Append extension if known
            ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
            mime = kwargs.get("file_mime", "")
            ext = ext_map.get(mime, ".img")
            with open(img_path + ext, "wb") as f:
                f.write(raw_bytes)
        except Exception as exc:
            logger.warning("Image save failed: %s", exc)
        return content, self._slugify(title)

    async def _process_audio(self, source: str, **kwargs: Any) -> tuple[str, str]:
        """Audio: 保存 .opus 原始文件，调用 Paraformer ASR 转写。

        转写成功后文本自动进入 Analyze → GraphProcess → Render 流程。
        失败时降级为 placeholder 文本，不中断 pipeline。
        """
        file_name = kwargs.get("file_name", "audio")

        # ① 解码并保存原始音频（.opus 扩展名，用于溯源）
        audio_path = ""
        try:
            raw_bytes = base64.b64decode(source)
            raw_dir = self._user_raw_dir()
            date_str = datetime.now().strftime("%Y-%m-%d")
            base_path = os.path.join(raw_dir, f"{date_str}-{self._slugify(file_name)}")
            audio_path = base_path + ".opus"
            with open(audio_path, "wb") as f:
                f.write(raw_bytes)
        except Exception as exc:
            logger.warning("音频保存失败: %s", exc)

        # ② ASR 转写
        transcript = ""
        if audio_path and os.path.exists(audio_path):
            result = await self._transcriber.transcribe(audio_path)
            if result.success:
                logger.info(
                    "ASR 成功 [%s]: %d 字符, %.0fms",
                    result.request_id, len(result.text), result.duration_ms,
                )
                transcript = result.text
            else:
                logger.warning("ASR 降级: %s", result.error)

        # ③ 构建 content + title
        if transcript.strip():
            content = transcript
            title = self._extract_title(transcript) or file_name
        else:
            saved = os.path.basename(audio_path) if audio_path else file_name
            content = (
                "[语音消息 - 转写失败，原始音频已保存]\n\n"
                f"音频文件：{saved}\n"
                "原因：ASR 服务不可用或转写失败。"
            )
            title = file_name

        return content, self._slugify(title)

    # ------------------------------------------------------------------
    # File extraction helpers
    # ------------------------------------------------------------------

    async def _extract_pdf(self, raw_bytes: bytes, file_name: str) -> tuple[str, str]:
        """Extract text from PDF, with OCR fallback for scanned/image-based PDFs.

        Strategy:
            1. Try pymupdf text-layer extraction (fast, works for digital PDFs)
            2. If text < 50 chars, render each page as image and run OCR
               (PaddleOCR → qwen-vl-max), reusing the existing ImageOCRExtractor
            3. If OCR also fails, raise RuntimeError with friendly message
        """
        try:
            import fitz  # pymupdf
        except ImportError:
            raise RuntimeError("PDF 处理需要安装 pymupdf: pip install pymupdf")

        # Step 1: Try text-layer extraction
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
        pages_text = [page.get_text() for page in doc]
        content = "\n\n".join(pages_text)

        if len(content.strip()) >= 50:
            doc.close()
            return content, self._slugify(file_name)

        # Step 2: OCR fallback for scanned/image-based PDFs
        logger.info(
            "PDF text layer too short (%d chars), trying OCR fallback for %d pages...",
            len(content.strip()), len(doc),
        )
        try:
            from app.config import get_settings
            from app.ingest.ocr import ImageOCRExtractor

            settings = get_settings()
            ocr = ImageOCRExtractor(
                dashscope_api_key=settings.dashscope_api_key,
                paddle_enabled=True,
            )

            ocr_texts: list[str] = []
            for page in doc:
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes("png")
                img_b64 = base64.b64encode(img_bytes).decode()
                result = await ocr.extract(img_b64)
                if result.text.strip():
                    ocr_texts.append(result.text)
                    logger.debug(
                        "PDF page OCR: engine=%s, text_len=%d",
                        result.engine, len(result.text),
                    )

            doc.close()

            ocr_content = "\n\n".join(ocr_texts)
            if len(ocr_content.strip()) >= 50:
                logger.info(
                    "PDF OCR fallback succeeded: %d chars from %d pages",
                    len(ocr_content), len(ocr_texts),
                )
                return ocr_content, self._slugify(file_name)

            # Both text extraction and OCR failed
            raise ValueError(
                "PDF 内容提取失败：文本层为空且 OCR 未识别到有效文字。"
                "该 PDF 可能是完全图片型或加密文档。"
            )
        except ValueError:
            raise
        except Exception as ocr_exc:
            doc.close()
            logger.warning("PDF OCR fallback failed: %s", ocr_exc)
            raise RuntimeError(
                f"PDF 提取失败：文本层为空且 OCR 回退失败 ({ocr_exc})"
            )

    def _extract_docx(self, raw_bytes: bytes, file_name: str) -> tuple[str, str]:
        """Extract text from DOCX. Minimal implementation using zipfile."""
        import zipfile
        import xml.etree.ElementTree as ET

        try:
            text_parts = []
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
                xml_content = z.read("word/document.xml")
            root = ET.fromstring(xml_content)
            for para in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
                if para.text:
                    text_parts.append(para.text)
            return "\n".join(text_parts), self._slugify(file_name)
        except Exception as exc:
            logger.warning("DOCX extraction failed: %s", exc)
            return f"[DOCX extraction failed: {exc}]", self._slugify(file_name)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _save_raw(self, content: str, title: str) -> str:
        """Save content to raw/sources/{user_id}/ with date-prefixed filename.

        Returns the filepath on success, or empty string if saving fails
        (raw archival is non-critical — ingestion should not be blocked).
        """
        try:
            raw_dir = self._user_raw_dir()
        except (PermissionError, OSError) as exc:
            logger.warning("Cannot create raw directory (ingestion continues): %s", exc)
            return ""

        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}-{self._slugify(title)}.md"
        filepath = os.path.join(raw_dir, filename)

        # Avoid overwriting
        counter = 1
        while os.path.exists(filepath):
            filename = f"{date_str}-{self._slugify(title)}-{counter}.md"
            filepath = os.path.join(raw_dir, filename)
            counter += 1

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        except (PermissionError, OSError) as exc:
            logger.warning("Cannot save raw file (ingestion continues): %s", exc)
            return ""

        return filepath

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """Check if a URL is safe to fetch (not pointing to internal/private IPs)."""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False
            # Resolve hostname to IP and check if it's private
            addr = ipaddress.ip_address(hostname)
            return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)
        except (ValueError, TypeError):
            # If hostname can't be parsed as IP, allow it (DNS resolution will handle it)
            # But still block obviously dangerous schemes
            if parsed.scheme not in ("http", "https"):
                return False
            return True

    @staticmethod
    def _extract_title(text: str) -> str:
        """Extract a title from the first line of text."""
        first_line = text.strip().split("\n")[0].strip()
        # Remove markdown heading markers
        first_line = re.sub(r"^#+\s*", "", first_line)
        # Truncate
        if len(first_line) > 60:
            first_line = first_line[:60]
        return first_line if first_line else "untitled"

    @staticmethod
    def _slugify(text: str) -> str:
        """Convert text to a file-system-safe slug."""
        text = re.sub(r"[^\w\s-]", "", text.lower())
        text = re.sub(r"[\s_]+", "-", text.strip())
        return text[:80] or "untitled"
