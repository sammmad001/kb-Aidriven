"""Intent detection: classify user text as QUERY or INPUT.

Three-layer architecture:
  ① Explicit command prefix (/q, /search) → handled by parse_command, not here
  ② Rule engine (synchronous, 0ms) — strong signals for query vs input
  ③ LLM quick judgment (qwen-turbo, ~300ms, timeout=2s) — for ambiguous cases

Safety bias: when in doubt, treat as INPUT (safe default).
Misclassifying a query as input just means the user types /q once more.
Misclassifying an input as a query causes silent data loss.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from app.llm import LLMClient

logger = logging.getLogger(__name__)

IntentType = Literal["query", "input"]

# ---------------------------------------------------------------------------
# Rule-based signal patterns
# ---------------------------------------------------------------------------

# Strong query signals: interrogative endings or question words
_QUERY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[？?]\s*$"),                        # ends with ? or ？
    re.compile(r"^(什么是|什么是|怎么|如何|为什么|为何|哪里|哪个|哪些|多少|是不是|有没有|能不能|可不可以|会不会)"),
    re.compile(r"^(what|how|why|where|which|who|when|is|are|can|do|does)\b", re.IGNORECASE),
    re.compile(r"(区别|差异|对比|比较|关系|联系|原因|原理|定义|介绍|解释)"),
]

# Follow-up reference words (require active context to be query)
_FOLLOWUP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(它的|它的|这个|那个|该|此|前面提到的|刚才说的)"),
    re.compile(r"(呢|嘛|吗|吧)\s*[？?]?\s*$"),
]

# Strong input signals: long text, multi-paragraph, URLs
_INPUT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://\S+"),                     # contains URL
]

_INPUT_MIN_LENGTH = 100  # characters


class IntentDetector:
    """Three-layer intent detector: rules first, LLM fallback for ambiguous cases."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        llm_model: str = "qwen-turbo",
        llm_timeout: float = 2.0,
    ) -> None:
        self._llm = llm
        self._llm_model = llm_model
        self._llm_timeout = llm_timeout

    async def detect(self, text: str, has_recent_query: bool = False) -> IntentType:
        """Classify text as 'query' or 'input'.

        Args:
            text: The user's raw text (without command prefix).
            has_recent_query: Whether the user has an active conversation context.
                              When True, follow-up references are more likely to be queries.

        Returns:
            'query' or 'input'. Defaults to 'input' on any ambiguity.
        """
        stripped = text.strip()
        if not stripped:
            return "input"

        # Layer 2a: Strong query signals (immediate, 0ms)
        for pattern in _QUERY_PATTERNS:
            if pattern.search(stripped):
                return "query"

        # Layer 2b: Strong input signals
        if len(stripped) > _INPUT_MIN_LENGTH:
            return "input"
        if "\n" in stripped:
            # Multi-paragraph text is very likely input
            return "input"
        for pattern in _INPUT_PATTERNS:
            if pattern.search(stripped):
                return "input"

        # Layer 2c: Follow-up reference words with active context
        if has_recent_query:
            for pattern in _FOLLOWUP_PATTERNS:
                if pattern.search(stripped):
                    return "query"

        # Layer 3: LLM quick judgment for ambiguous cases
        if self._llm:
            return await self._llm_detect(stripped)

        # No LLM available → safe default
        return "input"

    async def _llm_detect(self, text: str) -> IntentType:
        """Use a fast LLM call to classify ambiguous text.

        Timeout or any error → 'input' (safe fallback).
        """
        system_prompt = (
            "你是一个意图分类器。判断用户输入是要查询知识库还是存入知识库。\n"
            "查询：提问、寻求信息、寻找关系。\n"
            "存入：分享知识、笔记、陈述事实、记录信息。\n"
            "只输出一个词：QUERY 或 INPUT"
        )
        user_prompt = f"用户输入：{text[:200]}\n\n请判断意图，只输出 QUERY 或 INPUT："

        try:
            raw = await asyncio.wait_for(
                self._llm.chat(system_prompt, user_prompt, model=self._llm_model),
                timeout=self._llm_timeout,
            )
            result = raw.strip().upper()
            if "QUERY" in result:
                return "query"
            if "INPUT" in result:
                return "input"
            # Unparseable LLM output → safe default
            logger.debug("LLM intent unclear: %s", raw[:50])
            return "input"
        except asyncio.TimeoutError:
            logger.debug("LLM intent detection timed out → input (safe default)")
            return "input"
        except Exception as exc:
            logger.debug("LLM intent detection failed: %s → input (safe default)", exc)
            return "input"
