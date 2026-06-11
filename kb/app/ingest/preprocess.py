"""Step 1: Preprocess various input formats into unified Markdown."""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.models import InputFormat, PreprocessResult

logger = logging.getLogger(__name__)


class Preprocessor:
    """Convert various input formats into unified Markdown and save to raw/sources/."""

    def __init__(self, settings: Settings) -> None:
        self._raw_dir = settings.raw_dir
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        os.makedirs(self._raw_dir, exist_ok=True)

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
        content = f"[Image uploaded - OCR pending]\n\nImage data stored in raw sources."
        # Save the raw image
        try:
            raw_bytes = base64.b64decode(source)
            img_path = os.path.join(self._raw_dir, f"{datetime.now().strftime('%Y-%m-%d')}-{self._slugify(title)}")
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
        """Audio: save raw, mark as Whisper transcription placeholder."""
        title = kwargs.get("file_name", "audio")
        content = f"[Audio uploaded - Whisper transcription pending]\n\nAudio data stored in raw sources."
        try:
            raw_bytes = base64.b64decode(source)
            audio_path = os.path.join(self._raw_dir, f"{datetime.now().strftime('%Y-%m-%d')}-{self._slugify(title)}")
            with open(audio_path + ".bin", "wb") as f:
                f.write(raw_bytes)
        except Exception as exc:
            logger.warning("Audio save failed: %s", exc)
        return content, self._slugify(title)

    # ------------------------------------------------------------------
    # File extraction helpers
    # ------------------------------------------------------------------

    async def _extract_pdf(self, raw_bytes: bytes, file_name: str) -> tuple[str, str]:
        """Extract text from PDF using pymupdf."""
        try:
            import fitz  # pymupdf
        except ImportError:
            raise RuntimeError("PDF 处理需要安装 pymupdf: pip install pymupdf")

        try:
            doc = fitz.open(stream=raw_bytes, filetype="pdf")
            pages = [page.get_text() for page in doc]
            doc.close()
            content = "\n\n".join(pages)
            if len(content.strip()) < 50:
                raise ValueError("PDF 提取内容为空或过短")
            return content, self._slugify(file_name)
        except Exception as exc:
            logger.warning("PDF extraction failed: %s", exc)
            raise RuntimeError(f"PDF 提取失败: {exc}")

    def _extract_docx(self, raw_bytes: bytes, file_name: str) -> tuple[str, str]:
        """Extract text from DOCX. Minimal implementation using zipfile."""
        import zipfile
        import xml.etree.ElementTree as ET

        try:
            text_parts = []
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
                xml_content = z.read("word/document.xml")
            root = ET.fromstring(xml_content)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
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
        """Save content to raw/sources/ with date-prefixed filename."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}-{self._slugify(title)}.md"
        filepath = os.path.join(self._raw_dir, filename)

        # Avoid overwriting
        counter = 1
        while os.path.exists(filepath):
            filename = f"{date_str}-{self._slugify(title)}-{counter}.md"
            filepath = os.path.join(self._raw_dir, filename)
            counter += 1

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

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
