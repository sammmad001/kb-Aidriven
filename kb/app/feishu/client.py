"""Feishu API client: token management, message sending, file downloading."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn"


class FeishuClient:
    """Async Feishu Open API client with automatic token management."""

    def __init__(self, settings: Settings) -> None:
        self._app_id = settings.feishu_app_id
        self._app_secret = settings.feishu_app_secret
        self._http = httpx.AsyncClient(timeout=30.0)
        self._token: str = ""
        self._token_expires: float = 0.0

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Get or refresh tenant_access_token."""
        if self._token and time.time() < self._token_expires - 300:
            return self._token

        resp = await self._http.post(
            f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: {data.get('msg', 'unknown')}")

        self._token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200)
        logger.info("Feishu token refreshed, expires in %ds", data.get("expire", 7200))
        return self._token

    async def _headers(self) -> dict[str, str]:
        """Build authorization headers."""
        token = await self._ensure_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    async def reply_message(
        self, message_id: str, content: dict[str, Any], msg_type: str = "interactive",
    ) -> str:
        """Reply to a message with a card or text."""
        import json as _json

        headers = await self._headers()
        resp = await self._http.post(
            f"{FEISHU_BASE}/open-apis/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={
                "msg_type": msg_type,
                "content": _json.dumps(content) if isinstance(content, dict) else content,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Feishu reply failed: %s", data)
        return data.get("data", {}).get("message_id", "")

    async def reply_text(self, message_id: str, text: str) -> str:
        """Reply with a plain text message (lightweight instant feedback)."""
        return await self.reply_message(message_id, {"text": text}, msg_type="text")

    async def send_message(
        self, chat_id: str, content: dict[str, Any], msg_type: str = "interactive",
    ) -> str:
        """Send a message to a chat."""
        import json as _json

        headers = await self._headers()
        resp = await self._http.post(
            f"{FEISHU_BASE}/open-apis/im/v1/messages",
            headers=headers,
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": _json.dumps(content) if isinstance(content, dict) else content,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("message_id", "")

    async def update_message(
        self, message_id: str, content: dict[str, Any],
    ) -> None:
        """Update an existing message (for progressive feedback)."""
        import json as _json

        headers = await self._headers()
        resp = await self._http.patch(
            f"{FEISHU_BASE}/open-apis/im/v1/messages/{message_id}",
            headers=headers,
            json={"content": _json.dumps(content) if isinstance(content, dict) else content},
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # File/Resource operations
    # ------------------------------------------------------------------

    async def download_resource(self, message_id: str, file_key: str, resource_type: str = "file") -> bytes:
        """Download a file/image/audio resource from Feishu."""
        token = await self._ensure_token()
        resp = await self._http.get(
            f"{FEISHU_BASE}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {token}"},
            params={"type": resource_type},
        )
        resp.raise_for_status()
        return resp.content
