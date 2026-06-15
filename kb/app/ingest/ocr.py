"""Image OCR extraction with PaddleOCR (free primary) + qwen-vl-max (paid fallback).

Strategy:
    1. Try PaddleOCR first — free, local, ~80% accuracy on Chinese text
    2. If PaddleOCR confidence < threshold, fall back to DashScope qwen-vl-max
    3. If no DashScope key configured, return PaddleOCR result anyway
    
Dependencies (optional — only required if using the corresponding engine):
    - paddleocr + paddlepaddle  (free, local)
    - openai (DashScope-compatible)  (paid, cloud)
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from app.models import OCRResult

logger = logging.getLogger(__name__)

# Confidence threshold below which we fall back to qwen-vl
_PADDLE_CONFIDENCE_THRESHOLD = 0.6


class ImageOCRExtractor:
    """Dual-engine OCR: PaddleOCR (free) → qwen-vl-max (fallback)."""

    def __init__(self, dashscope_api_key: str = "", paddle_enabled: bool = True) -> None:
        """
        Args:
            dashscope_api_key: DashScope API key for qwen-vl-max fallback.
                If empty, only PaddleOCR is used.
            paddle_enabled: Whether to enable PaddleOCR.
        """
        self._dashscope_key = dashscope_api_key
        self._paddle_enabled = paddle_enabled
        self._paddle: Any = None
        self._paddle_available = False

        if paddle_enabled:
            try:
                from paddleocr import PaddleOCR  # type: ignore[import-untyped]
                self._paddle = PaddleOCR(lang="ch", use_angle_cls=True)
                self._paddle_available = True
                logger.info("PaddleOCR initialized (free local OCR engine)")
            except ImportError:
                logger.warning(
                    "PaddleOCR not installed. Install with: "
                    "pip install paddlepaddle paddleocr. "
                    "OCR will require DashScope fallback."
                )
            except Exception as exc:
                logger.warning("PaddleOCR init failed: %s. Falling back to API-only.", exc)

    async def extract(self, image_base64: str) -> OCRResult:
        """Extract text from a base64-encoded image.

        Returns OCRResult with text, engine name, confidence, and duration.
        """
        t0 = time.monotonic()

        # --- Path 1: PaddleOCR (free) ---
        if self._paddle_available:
            try:
                image_bytes = base64.b64decode(image_base64)
                # PaddleOCR is sync; run in thread to avoid blocking
                import asyncio
                result = await asyncio.to_thread(self._paddle.ocr, image_bytes)
                text, confidence = self._format_paddle_result(result)

                duration_ms = (time.monotonic() - t0) * 1000
                if confidence >= _PADDLE_CONFIDENCE_THRESHOLD and len(text.strip()) > 10:
                    logger.debug(
                        "PaddleOCR succeeded: confidence=%.2f, text_len=%d, %.0fms",
                        confidence, len(text), duration_ms,
                    )
                    return OCRResult(
                        text=text, engine="paddle",
                        confidence=round(confidence, 4), duration_ms=round(duration_ms, 1),
                    )
                # Confidence too low — fall through to qwen-vl
                logger.debug(
                    "PaddleOCR low confidence (%.2f), trying qwen-vl fallback", confidence,
                )
            except Exception as exc:
                logger.warning("PaddleOCR failed: %s. Trying fallback.", exc)

        # --- Path 2: qwen-vl-max fallback (paid) ---
        if self._dashscope_key:
            try:
                text = await self._extract_with_qwen_vl(image_base64)
                duration_ms = (time.monotonic() - t0) * 1000
                return OCRResult(
                    text=text, engine="qwen-vl",
                    confidence=1.0, duration_ms=round(duration_ms, 1),
                )
            except Exception as exc:
                logger.warning("qwen-vl-max OCR also failed: %s", exc)
                # If both fail, return whatever PaddleOCR gave (even if low confidence)
                if self._paddle_available:
                    try:
                        image_bytes = base64.b64decode(image_base64)
                        import asyncio
                        result = await asyncio.to_thread(self._paddle.ocr, image_bytes)
                        text, confidence = self._format_paddle_result(result)
                        duration_ms = (time.monotonic() - t0) * 1000
                        return OCRResult(
                            text=text, engine="paddle",
                            confidence=round(confidence, 4), duration_ms=round(duration_ms, 1),
                        )
                    except Exception:
                        pass
                return OCRResult(
                    text="", engine="none", confidence=0.0,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        # --- No fallback available — return PaddleOCR result as-is ---
        if self._paddle_available:
            try:
                image_bytes = base64.b64decode(image_base64)
                import asyncio
                result = await asyncio.to_thread(self._paddle.ocr, image_bytes)
                text, confidence = self._format_paddle_result(result)
                duration_ms = (time.monotonic() - t0) * 1000
                return OCRResult(
                    text=text, engine="paddle",
                    confidence=round(confidence, 4), duration_ms=round(duration_ms, 1),
                )
            except Exception:
                pass

        # --- Nothing worked ---
        return OCRResult(
            text="", engine="none", confidence=0.0,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    async def extract_batch(self, images_base64: list[str]) -> list[OCRResult]:
        """Extract text from multiple images in parallel."""
        if not images_base64:
            return []
        import asyncio
        tasks = [self.extract(img) for img in images_base64]
        return await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_paddle_result(self, result: Any) -> tuple[str, float]:
        """Format PaddleOCR result into plain text and average confidence.

        PaddleOCR returns: [[[bbox, (text, confidence)], ...], ...]
        One sub-list per detected text block.
        """
        if not result or not result[0]:
            return "", 0.0

        lines: list[str] = []
        confidences: list[float] = []

        for block in result[0]:
            text = str(block[1][0]) if len(block) >= 2 and len(block[1]) >= 1 else ""
            conf = float(block[1][1]) if len(block) >= 2 and len(block[1]) >= 2 else 0.0
            if text.strip():
                lines.append(text)
                confidences.append(conf)

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return "\n".join(lines), avg_conf

    async def _extract_with_qwen_vl(self, image_base64: str) -> str:
        """Use DashScope qwen-vl-max for OCR on a single image."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self._dashscope_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        response = await client.chat.completions.create(
            model="qwen-vl-max",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                        {
                            "type": "text",
                            "text": (
                                "请提取这张图片中的所有文字内容。\n"
                                "- 如果是表格，保留表格结构\n"
                                "- 如果是代码，保留代码格式\n"
                                "- 如果有图表标注，也请提取\n"
                                "- 直接输出文字，不要添加解释"
                            ),
                        },
                    ],
                }
            ],
            max_tokens=2000,
            temperature=0.1,
        )

        content = response.choices[0].message.content or ""
        return content.strip()
