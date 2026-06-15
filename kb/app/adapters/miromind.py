"""MiroMind adapter: transforms MiroMind deep research results into KB ingest format."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.adapters.base import ExtractedKnowledge, KnowledgeAdapter

logger = logging.getLogger(__name__)

# Minimum tokens threshold for quality filtering
DEFAULT_MIN_TOKENS = 500


class MiroMindAdapter(KnowledgeAdapter):
    """Adapter for MiroMind deep research agent sessions.

    Expects raw payload containing session info + assistant message data.
    """

    def __init__(self, min_tokens: int = DEFAULT_MIN_TOKENS) -> None:
        self._min_tokens = min_tokens

    @property
    def source_type(self) -> str:
        return "miromind"

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------

    async def extract(self, source_data: dict[str, Any]) -> ExtractedKnowledge:
        """Extract structured knowledge from a MiroMind raw message payload.

        Expected source_data keys:
            session_id, message_id, session_title, session_model,
            content, thinking_text, tool_events, total_tokens,
            duration_ms, status, model
        """
        session_id = source_data.get("session_id", 0)
        message_id = source_data.get("message_id", 0)
        title = source_data.get("session_title", "MiroMind 研究")
        session_model = source_data.get("session_model", "")

        content = source_data.get("content", "")
        thinking_text = source_data.get("thinking_text", "")
        tool_events = source_data.get("tool_events", [])

        # Parse tool_events if it's a JSON string
        if isinstance(tool_events, str):
            try:
                tool_events = json.loads(tool_events)
            except (json.JSONDecodeError, TypeError):
                tool_events = []

        # Build structured Markdown content
        markdown_parts: list[str] = []

        # Title
        markdown_parts.append(f"# {title}\n")

        # Main content
        markdown_parts.append(content.strip())

        # Append thinking as collapsible section
        if thinking_text.strip():
            markdown_parts.append("\n---\n## 思考过程\n")
            markdown_parts.append(thinking_text.strip())

        # Append tool events summary
        if tool_events:
            markdown_parts.append("\n---\n## 研究过程\n")
            for evt in tool_events:
                evt_type = evt.get("type", evt.get("name", ""))
                if evt_type == "search":
                    keywords = evt.get("keywords", [])
                    if keywords:
                        markdown_parts.append(f"- 🔍 搜索: {', '.join(keywords)}")
                elif evt_type == "fetch":
                    url = evt.get("content", evt.get("url", ""))
                    if url:
                        markdown_parts.append(f"- 📄 获取: {url}")
                elif evt_type == "python":
                    code = evt.get("content", "")
                    if code:
                        markdown_parts.append("- 🐍 Python 执行")
                elif evt_type == "tool_call":
                    name = evt.get("name", "")
                    markdown_parts.append(f"- 🔧 工具调用: {name}")
                else:
                    markdown_parts.append(f"- 📌 {evt_type}")

        # Footer metadata
        total_tokens = source_data.get("total_tokens", 0)
        duration_ms = source_data.get("duration_ms", 0)
        model = source_data.get("model", session_model)

        markdown_parts.append(
            f"\n---\n> 🤖 {model} | Token: {total_tokens} | "
            f"耗时: {duration_ms // 1000}s | "
            f"[原始对话](miromind://session/{session_id}#msg-{message_id})"
        )

        full_content = "\n".join(markdown_parts)

        metadata: dict[str, Any] = {
            "session_id": session_id,
            "message_id": message_id,
            "model": model,
            "session_model": session_model,
            "total_tokens": total_tokens,
            "duration_ms": duration_ms,
            "status": source_data.get("status", "completed"),
            "thinking_text_length": len(thinking_text),
            "tool_event_count": len(tool_events),
        }

        return ExtractedKnowledge(
            source_type=self.source_type,
            source_id=f"{session_id}:{message_id}",
            title=title,
            content=full_content,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    async def validate(
        self, extracted: ExtractedKnowledge
    ) -> tuple[bool, str]:
        """Validate that MiroMind research result meets quality thresholds.

        Filters out: too-short conversations, errors, thinking-only responses.
        """
        meta = extracted.metadata

        # Skip errored responses
        status = meta.get("status", "")
        if status == "error" or status == "interrupted":
            return False, f"message status is '{status}'"

        # Skip if no substantive content (thinking only, no final answer)
        content_text = extracted.content
        if not content_text or not content_text.strip():
            return False, "empty content"

        # Check token threshold
        total_tokens = meta.get("total_tokens", 0)
        if total_tokens < self._min_tokens:
            return False, (
                f"token count too low ({total_tokens} < {self._min_tokens})"
            )

        # Skip if content is only thinking (no final answer)
        main_content = meta.get("thinking_text_length", 0)
        if main_content and not content_text.replace(
            extracted.metadata.get("session_title", ""), ""
        ).strip():
            return False, "thinking only, no final answer content"

        return True, "ok"
