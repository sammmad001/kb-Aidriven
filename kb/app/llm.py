"""LLM abstraction layer supporting Ollama (local) and DashScope (API)."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract base class for LLM calls."""

    @abstractmethod
    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None) -> str:
        """Send a chat completion request and return the assistant text.
        
        Args:
            system: System prompt.
            user: User message.
            json_mode: Whether to request JSON output.
            model: Optional model override. If None, uses the default model.
        """

    async def chat_json(self, system: str, user: str, model: str | None = None) -> dict[str, Any]:
        """Send a chat request asking for JSON output, then parse it."""
        raw = await self.chat(system, user, json_mode=True, model=model)
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

    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None) -> str:
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
        return data.get("message", {}).get("content", "")


class DashScopeClient(LLMClient):
    """DashScope (Alibaba Cloud / 百炼) LLM client using OpenAI-compatible API."""

    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._default_model = model
        self._client = httpx.AsyncClient(timeout=120.0)

    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None) -> str:
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
        return data["choices"][0]["message"]["content"]


def get_llm_client(settings: Settings) -> LLMClient:
    """Factory: create the appropriate LLM client based on settings."""
    if settings.llm_provider == "dashscope":
        if not settings.dashscope_api_key:
            raise ValueError("DASHSCOPE_API_KEY is required when llm_provider=dashscope")
        return DashScopeClient(settings.dashscope_api_key, settings.dashscope_model)
    return OllamaClient(settings.ollama_base_url, settings.ollama_model)
