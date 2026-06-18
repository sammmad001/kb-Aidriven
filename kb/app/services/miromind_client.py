"""MiroMind API client: non-streaming deep research integration.

Calls MiroMind's Chat Completions API (OpenAI-compatible) with a single POST
to /v1/chat/completions to get a complete response in one HTTP round-trip.
Injects a fixed LENGTH_CONSTRAINT prompt to ensure the output stays concise
(≤2000 characters).

The result is converted to a MiroMindAdapter-compatible payload for ingestion
into the knowledge base pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Fixed prompt prefix injected into every research request.
# Ensures output is concise and well-structured.
LENGTH_CONSTRAINT = (
    "[Language] 请务必使用中文回答。\n"
    "[Output Requirement] Please keep your response concise and well-structured. "
    "The total output must not exceed 2000 characters. Focus on key findings "
    "and actionable insights. Use clear headings and bullet points."
)


@dataclass
class ResearchResult:
    """Result of a MiroMind deep research call."""

    content: str           # Main research body (≤2000 chars, constrained by prompt)
    thinking_text: str     # Reasoning summary extracted from response (if any)
    total_tokens: int      # Total tokens used
    status: str            # completed / error / failed
    model: str             # Model used
    duration_ms: int       # Request duration in milliseconds
    error: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)

    def to_miromind_payload(self) -> dict[str, Any]:
        """Convert to MiroMindAdapter-compatible payload format.

        Aligns with MiroMindMessagePayload fields in ingest_adapters.py.
        """
        # Generate pseudo session/message IDs for adapter compatibility
        ts = int(time.time())
        return {
            "session_id": ts,
            "message_id": ts,
            "session_title": "飞书深度研究",
            "session_model": self.model,
            "content": self.content,
            "thinking_text": self.thinking_text,
            "tool_events": self.tool_events,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "model": self.model,
        }


class MiroMindClient:
    """Non-streaming MiroMind API client (OpenAI Chat Completions format).

    Sends a single POST to /v1/chat/completions and waits for
    the complete JSON response. No SSE parsing required.
    """

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._api_base = api_base or settings.miromind_api_base
        self._api_key = api_key or settings.miromind_api_key
        self._default_model = default_model or settings.miromind_default_model
        self._timeout = timeout or settings.miromind_request_timeout

    @property
    def is_configured(self) -> bool:
        """Check if the client has required API key."""
        return bool(self._api_key)

    async def research(self, question: str, model: str | None = None) -> ResearchResult:
        """Execute a deep research query (non-streaming).

        Automatically injects LENGTH_CONSTRAINT prompt to limit output to ≤2000 chars.

        Args:
            question: The research question.
            model: Optional model override. Uses default if not specified.

        Returns:
            ResearchResult with the complete response.
        """
        if not self.is_configured:
            return ResearchResult(
                content="",
                thinking_text="",
                total_tokens=0,
                status="error",
                model=model or self._default_model,
                duration_ms=0,
                error="MIROMIND_API_KEY not configured",
            )

        # Inject fixed length constraint prompt into user message
        enriched_content = f"{LENGTH_CONSTRAINT}\n\n{question}"
        use_model = model or self._default_model
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": [
                {"role": "user", "content": enriched_content},
            ],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        t_start = time.monotonic()
        logger.info("MiroMind research started: model=%s question=%s...", use_model, question[:50])

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._api_base}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                duration_ms = int((time.monotonic() - t_start) * 1000)

                if resp.status_code != 200:
                    error_text = resp.text[:500]
                    logger.error("MiroMind API error: %d %s", resp.status_code, error_text)
                    return ResearchResult(
                        content="",
                        thinking_text="",
                        total_tokens=0,
                        status="error",
                        model=use_model,
                        duration_ms=duration_ms,
                        error=f"HTTP {resp.status_code}: {error_text}",
                    )

                # Guard: empty response body (MiroMind may return 200 with empty body)
                raw_text = resp.text
                if not raw_text or not raw_text.strip():
                    logger.error(
                        "MiroMind API returned 200 with empty body "
                        "(content-type=%s, content-length=%s)",
                        resp.headers.get("content-type"),
                        resp.headers.get("content-length"),
                    )
                    return ResearchResult(
                        content="",
                        thinking_text="",
                        total_tokens=0,
                        status="error",
                        model=use_model,
                        duration_ms=duration_ms,
                        error="MiroMind API 返回了空响应（服务器可能内部错误，请稍后重试）",
                    )

                # Detect SSE streaming response (starts with "data:")
                if raw_text.lstrip().startswith("data:"):
                    logger.info("MiroMind returned SSE stream, parsing chunks...")
                    data = self._parse_sse_response(raw_text)
                    if data is None:
                        return ResearchResult(
                            content="",
                            thinking_text="",
                            total_tokens=0,
                            status="error",
                            model=use_model,
                            duration_ms=duration_ms,
                            error="MiroMind API 返回了流式响应但未能解析",
                        )
                    return self._parse_response(data, use_model, duration_ms)

                # Standard JSON response
                try:
                    data = resp.json()
                except Exception:
                    logger.error(
                        "MiroMind API JSON parse failed. "
                        "Raw response (first 500 chars): %s",
                        raw_text[:500],
                    )
                    return ResearchResult(
                        content="",
                        thinking_text="",
                        total_tokens=0,
                        status="error",
                        model=use_model,
                        duration_ms=duration_ms,
                        error=f"MiroMind API 响应格式错误（非 JSON），原始响应前200字符: {raw_text[:200]}",
                    )
                return self._parse_response(data, use_model, duration_ms)

        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            logger.error("MiroMind API timeout after %.1fs", self._timeout)
            return ResearchResult(
                content="",
                thinking_text="",
                total_tokens=0,
                status="error",
                model=use_model,
                duration_ms=duration_ms,
                error=f"Request timed out after {self._timeout}s",
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            logger.exception("MiroMind research failed")
            return ResearchResult(
                content="",
                thinking_text="",
                total_tokens=0,
                status="error",
                model=use_model,
                duration_ms=duration_ms,
                error=str(exc),
            )

    def _parse_response(
        self, data: dict[str, Any], model: str, duration_ms: int
    ) -> ResearchResult:
        """Parse the Chat Completions JSON response from MiroMind API.

        Expected response structure (OpenAI Chat Completions format):
        {
            "id": "...",
            "model": "...",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "..."
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {"total_tokens": 1234, ...}
        }
        """
        # Extract content from choices[0].message.content
        choices = data.get("choices", [])
        content = ""
        if isinstance(choices, list) and len(choices) > 0:
            message = choices[0].get("message", {})
            content = message.get("content", "") or ""

        # Check for error in response body
        error_info = data.get("error")
        if error_info:
            return ResearchResult(
                content="",
                thinking_text="",
                total_tokens=data.get("usage", {}).get("total_tokens", 0),
                status="error",
                model=data.get("model", model),
                duration_ms=duration_ms,
                error=str(error_info),
            )

        total_tokens = data.get("usage", {}).get("total_tokens", 0)
        resp_model = data.get("model", model)

        logger.info(
            "MiroMind research completed: tokens=%d duration=%dms content_len=%d",
            total_tokens, duration_ms, len(content),
        )

        return ResearchResult(
            content=content.strip(),
            thinking_text="",
            total_tokens=total_tokens,
            status="completed",
            model=resp_model,
            duration_ms=duration_ms,
            tool_events=[],
        )

    def _parse_sse_response(self, raw_text: str) -> dict[str, Any] | None:
        """Parse SSE (Server-Sent Events) streaming response into a single dict.

        Handles `data: {json}` lines, concatenating delta content chunks
        into a single Chat Completions-compatible response.
        """
        import json

        content_parts: list[str] = []
        total_tokens = 0
        model_name = ""

        for line in raw_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if payload_str == "[DONE]":
                break
            try:
                chunk = json.loads(payload_str)
            except Exception:
                continue
            if not model_name:
                model_name = chunk.get("model", "")
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                text = delta.get("content", "")
                if text:
                    content_parts.append(text)
            usage = chunk.get("usage", {})
            if usage:
                total_tokens = usage.get("total_tokens", total_tokens)

        if not content_parts:
            return None

        return {
            "model": model_name,
            "choices": [
                {
                    "message": {"content": "".join(content_parts)},
                }
            ],
            "usage": {"total_tokens": total_tokens} if total_tokens else {},
        }

    async def health_check(self) -> bool:
        """Quick health check: verify API key is configured."""
        return self.is_configured
