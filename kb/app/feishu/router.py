"""Feishu webhook router: HTTP endpoint for event subscription."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response

import lark_oapi as lark
from lark_oapi.core.model import RawRequest
from app.config import get_settings
from app.feishu.handlers import _dedup, dispatch_message

logger = logging.getLogger(__name__)

router = APIRouter()

# Global event handler (initialized when settings are available)
_event_handler: lark.EventDispatcherHandler | None = None


def init_event_handler() -> lark.EventDispatcherHandler:
    """Create and return the event dispatcher handler with encryption keys."""
    global _event_handler
    if _event_handler is None:
        settings = get_settings()
        _event_handler = (
            lark.EventDispatcherHandler.builder(
                settings.feishu_verification_token,
                settings.feishu_encrypt_key,
            )
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )
    return _event_handler


def _on_message(data: Any) -> None:
    """Handle incoming message event from webhook."""
    try:
        message = data.event.message
        msg_type = message.message_type
        message_id = message.message_id

        # Dedup check: skip if this message_id was already processed
        if _dedup.is_duplicate(message_id):
            logger.info("Duplicate message_id %s, skipping", message_id)
            return

        content_str = message.content

        try:
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except json.JSONDecodeError:
            content = {}

        # Dispatch to async handler
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(dispatch_message(msg_type, content, message_id))
    except Exception as exc:
        logger.exception("Failed to handle webhook message: %s", exc)


@router.post("/webhook/feishu")
async def feishu_webhook(request: Request) -> Response:
    """Feishu webhook endpoint using lark-oapi SDK adapter.

    Feishu sends POST requests to this endpoint when events occur.
    Configure this URL in Feishu Developer Console:
        事件与回调 -> 事件订阅 -> 请求地址
        e.g. http://43.106.12.79:8080/webhook/feishu
    """
    body = await request.body()

    # Log request details for debugging
    logger.info("Webhook request: headers=%s, body_len=%d", 
                {k: v for k, v in request.headers.items() if k.lower() in ('content-type', 'x-lark-request-timestamp', 'x-lark-request-nonce')},
                len(body))
    
    # Handle URL verification challenge BEFORE SDK processing
    # (url_verification is sent in plain text, not encrypted)
    try:
        data = json.loads(body)
        logger.info("Webhook request type: %s", data.get('type', 'unknown'))
        if data.get("type") == "url_verification":
            challenge = data.get("challenge", "")
            logger.info("Webhook URL verification challenge received")
            return Response(
                content=json.dumps({"challenge": challenge}),
                status_code=200,
                media_type="application/json",
            )
        # Check if body has 'encrypt' field (encrypted request)
        if "encrypt" in data:
            logger.info("Webhook encrypted request detected, encrypt field length: %d", len(data.get("encrypt", "")))
            logger.info("Webhook encrypt field preview: %s...", data.get("encrypt", "")[:50])
    except json.JSONDecodeError:
        logger.warning("Webhook request is not valid JSON: %s", body[:100] if body else 'empty')
        pass  # Not JSON, let SDK handle it

    # Create SDK RawRequest object for encrypted events
    raw_req = RawRequest()
    raw_req.uri = str(request.url)
    raw_req.headers = dict(request.headers)
    raw_req.body = body

    # Use SDK's event handler to process encrypted events
    handler = init_event_handler()
    resp = handler.do(raw_req)

    # Return SDK response as FastAPI Response
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type="application/json",
    )
