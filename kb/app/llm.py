"""LLM abstraction layer supporting Ollama (local) and DashScope (API).

V1.1: Added ResilientLLMClient with model fallback chain and exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings

if TYPE_CHECKING:
    from app.models import TokenUsage

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract base class for LLM calls."""

    @abstractmethod
    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None, _usage: "TokenUsage | None" = None) -> str:
        """Send a chat completion request and return the assistant text.
        
        Args:
            system: System prompt.
            user: User message.
            json_mode: Whether to request JSON output.
            model: Optional model override. If None, uses the default model.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close underlying HTTP client resources."""

    async def chat_json(self, system: str, user: str, model: str | None = None, _usage: "TokenUsage | None" = None) -> dict[str, Any]:
        """Send a chat request asking for JSON output, then parse it."""
        raw = await self.chat(system, user, json_mode=True, model=model, _usage=_usage)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Try to extract and parse JSON from LLM output."""
        # Try direct parse first
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try to extract JSON from markdown code block
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass
        # Try to find first { ... } block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        logger.warning("Failed to parse JSON from LLM output: %s", text[:200])
        return {"raw": text, "_parse_error": True}


class OllamaClient(LLMClient):
    """Ollama local LLM client."""

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(timeout=120.0)

    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None, _usage: "TokenUsage | None" = None) -> str:
        effective_model = model or self._model
        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"

        resp = await self._client.post(
            f"{self._base_url}/api/chat",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama does not provide token counts; _usage remains as-is
        return data.get("message", {}).get("content", "")

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()


class DashScopeClient(LLMClient):
    """DashScope (Alibaba Cloud / 百炼) LLM client using OpenAI-compatible API."""

    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._default_model = model
        self._client = httpx.AsyncClient(timeout=120.0)

    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None, _usage: "TokenUsage | None" = None) -> str:
        effective_model = model or self._default_model
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = await self._client.post(
            f"{self.BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        # V1.2: Extract token usage from DashScope API response
        if _usage is not None:
            u = data.get("usage", {})
            _usage.prompt_tokens = u.get("prompt_tokens", 0)
            _usage.completion_tokens = u.get("completion_tokens", 0)
            _usage.total_tokens = u.get("total_tokens", 0)
        return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()


class ResilientLLMClient(LLMClient):
    """V1.1: LLM client wrapper with fallback chain and exponential backoff.

    Wraps an underlying LLMClient (Ollama or DashScope) and adds:
    - Primary model with configurable fallback tiers
    - Exponential backoff on transient failures
    - Automatic retry on timeout/5xx errors
    """

    # Fallback tiers: (model_name, max_retries, base_backoff_seconds)
    _DASHSCOPE_FALLBACKS: list[tuple[str, int, float]] = [
        ("qwen3.5-plus", 1, 1.0),
        ("qwen-flash", 1, 2.0),
        ("qwen-turbo", 1, 4.0),
    ]
    _OLLAMA_FALLBACK: str = ""  # Ollama has only one model typically

    def __init__(self, inner: LLMClient, provider: str) -> None:
        self._inner = inner
        self._provider = provider

    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None, _usage: "TokenUsage | None" = None) -> str:
        """Chat with fallback: try primary model, then fallback tiers on failure.

        V1.2: When _usage is provided, token counts are accumulated across
        all successful fallback attempts.
        """
        effective_model = model
        last_error: Exception | None = None

        # Determine fallback tiers based on provider
        if self._provider == "dashscope" and model:
            tiers = [(model, 1, 1.0)] + [
                (m, r, b) for m, r, b in self._DASHSCOPE_FALLBACKS if m != model
            ]
        else:
            # No fallback for Ollama or when model is not specified
            return await self._inner.chat(system, user, json_mode, effective_model, _usage=_usage)

        # Accumulator for multi-call token tracking
        from app.models import TokenUsage
        acc = TokenUsage() if _usage is not None else None

        for tier_model, max_retries, base_backoff in tiers:
            for attempt in range(max_retries + 1):
                try:
                    call_usage = TokenUsage() if _usage is not None else None
                    result = await self._inner.chat(system, user, json_mode, tier_model, _usage=call_usage)
                    if acc is not None and call_usage is not None:
                        acc.add(call_usage)
                    return result
                except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    logger.warning(
                        "LLM fallback: model=%s attempt=%d/%d failed: %s",
                        tier_model, attempt + 1, max_retries + 1, exc,
                    )
                    if attempt < max_retries:
                        backoff = base_backoff * (2 ** attempt)
                        await asyncio.sleep(backoff)
                    continue

        # Write accumulated tokens back to caller's _usage before raising
        if _usage is not None and acc is not None:
            _usage.prompt_tokens = acc.prompt_tokens
            _usage.completion_tokens = acc.completion_tokens
            _usage.total_tokens = acc.total_tokens
        raise RuntimeError(f"All model tiers exhausted: {last_error}") from last_error

    async def close(self) -> None:
        """Close the underlying client."""
        await self._inner.close()


def get_llm_client(settings: Settings) -> LLMClient:
    """Factory: create the appropriate LLM client based on settings.

    V1.1: DashScope clients are automatically wrapped with ResilientLLMClient
    for fallback chain support on transient failures.
    """
    if settings.llm_provider == "dashscope":
        if not settings.dashscope_api_key:
            raise ValueError("DASHSCOPE_API_KEY is required when llm_provider=dashscope")
        inner = DashScopeClient(settings.dashscope_api_key, settings.dashscope_model)
        return ResilientLLMClient(inner, "dashscope")
    return OllamaClient(settings.ollama_base_url, settings.ollama_model)
