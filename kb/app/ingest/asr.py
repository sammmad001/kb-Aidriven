"""Audio Speech Recognition (ASR) via DashScope Paraformer.

飞书语音消息(Ogg/Opus) → paraformer-realtime-v2 非流式识别 → 文本

Dependencies (optional — only required if using ASR):
    - dashscope  (pip install dashscope)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from http import HTTPStatus

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "paraformer-realtime-v2"
_DEFAULT_FORMAT = "opus"          # 飞书 OPU = Ogg/Opus，无需转码
_DEFAULT_SAMPLE_RATE = 16000


@dataclass
class ASRResult:
    """ASR 转写结果，与 OCRResult 风格对齐。"""

    text: str = ""
    engine: str = "none"
    duration_ms: float = 0.0
    request_id: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return bool(self.text.strip()) and not self.error


class AudioTranscriber:
    """DashScope Paraformer speech-to-text（非流式本地文件识别）.

    Usage::

        transcriber = AudioTranscriber(dashscope_api_key="sk-xxx")
        result = await transcriber.transcribe("/path/to/audio.opus")
        if result.success:
            print(result.text)
    """

    def __init__(
        self,
        dashscope_api_key: str = "",
        model: str = _DEFAULT_MODEL,
    ) -> None:
        """
        Args:
            dashscope_api_key: DashScope API key. 为空时 ASR 禁用，
                所有调用直接返回降级结果。
            model: Paraformer 模型名称。
        """
        self._api_key = dashscope_api_key
        self._model = model
        self._available = bool(dashscope_api_key)
        if not self._available:
            logger.warning(
                "DASHSCOPE_API_KEY 未配置，ASR 已禁用。"
                "音频将仅保存原始文件 + placeholder 文本。",
            )

    async def transcribe(
        self,
        audio_path: str,
        *,
        format: str = _DEFAULT_FORMAT,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        language_hints: list[str] | None = None,
    ) -> ASRResult:
        """转写本地音频文件，返回 ASRResult。

        失败时返回空文本 + error，不抛异常。
        """
        if not self._available:
            return ASRResult(error="DASHSCOPE_API_KEY 未配置")

        t0 = time.monotonic()
        try:
            text, request_id = await asyncio.to_thread(
                self._recognize_file,
                audio_path,
                format,
                sample_rate,
                language_hints or ["zh", "en"],
            )
            return ASRResult(
                text=text,
                engine="paraformer",
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                request_id=request_id,
            )
        except ImportError:
            logger.warning(
                "dashscope SDK 未安装，ASR 不可用。请安装: pip install dashscope",
            )
            return ASRResult(error="dashscope SDK 未安装")
        except Exception as exc:
            logger.warning("ASR 失败: %s", exc)
            return ASRResult(
                error=str(exc),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _recognize_file(
        self,
        audio_path: str,
        fmt: str,
        sr: int,
        lang_hints: list[str],
    ) -> tuple[str, str]:
        """同步调用 DashScope Recognition.call（在线程中执行）。

        Returns:
            (transcribed_text, request_id)
        """
        import dashscope
        from dashscope.audio.asr import Recognition

        # pydantic-settings 只读 .env，不写回 os.environ，
        # 而 dashscope SDK 依赖全局 dashscope.api_key，需显式设置。
        dashscope.api_key = self._api_key

        # Recognition 对象不可复用（官方约束），每次调用新建实例。
        recognition = Recognition(
            model=self._model,
            format=fmt,
            sample_rate=sr,
            language_hints=lang_hints,
            callback=None,
        )
        result = recognition.call(audio_path)
        request_id = recognition.get_last_request_id()

        if result.status_code != HTTPStatus.OK:
            msg = getattr(result, "message", f"HTTP {result.status_code}")
            raise RuntimeError(f"ASR API 错误: {msg}")

        return self._extract_text(result), request_id

    @staticmethod
    def _extract_text(result: object) -> str:
        """从 RecognitionResult.get_sentence() 提取完整文本。

        非流式 call() 下 get_sentence() 可能返回 dict（单句）或 list（多句），
        此方法兼容两种情况，拼接为完整文本。
        """
        sentence = result.get_sentence()  # type: ignore[attr-defined]
        if isinstance(sentence, dict):
            return (sentence.get("text") or "").strip()
        if isinstance(sentence, list):
            return "".join(
                s.get("text", "") for s in sentence if isinstance(s, dict)
            ).strip()
        return ""
