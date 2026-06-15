"""Feishu webhook router: HTTP endpoint for event subscription.

Bypasses the lark-oapi SDK EventDispatcherHandler for encrypted events
because the SDK's internal parsing/dispatch has compatibility issues.
Instead we manually decrypt, parse, and dispatch the event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response

from app.config import get_settings
from app.feishu.handlers import _dedup, dispatch_message

logger = logging.getLogger(__name__)

router = APIRouter()

# Prevent GC of background dispatch tasks (RES-01 fix)
_background_tasks: set[asyncio.Task] = set()


# ======================================================================
# Event dispatch (replaces SDK EventDispatcherHandler)
# ======================================================================

async def _dispatch_encrypted_event(encrypted_data: str) -> dict[str, Any]:
    """Manually decrypt and dispatch an encrypted Feishu event.

    Returns a dict with the response to send back to Feishu.
    """
    settings = get_settings()
    encrypt_key = settings.feishu_encrypt_key

    if not encrypt_key:
        logger.error("Received encrypted event but FEISHU_ENCRYPT_KEY is empty")
        return {"code": 1, "msg": "encrypt key not configured"}

    # Step 1: Decrypt using SDK's own AESCipher (guaranteed compatible)
    try:
        from lark_oapi.core.utils.decryptor import AESCipher
        cipher = AESCipher(encrypt_key)
        decrypted_json = cipher.decrypt_str(encrypted_data)
        logger.info("Decryption succeeded via SDK AESCipher, length=%d", len(decrypted_json))
    except Exception as exc:
        logger.error("SDK AESCipher decryption failed: %s", exc)
        return {"code": 1, "msg": f"decryption failed: {exc}"}

    if not decrypted_json:
        logger.error("Decryption returned empty data")
        return {"code": 1, "msg": "decryption returned empty"}

    # Step 2: Parse the decrypted JSON
    # Log raw decrypted content for debugging
    logger.info("Raw decrypted length=%d", len(decrypted_json))
    logger.info("Raw decrypted repr: %r", decrypted_json[:500])
    logger.info("Raw decrypted bytes (first 100): %s", decrypted_json[:100].encode('utf-8', errors='replace')[:100])
    try:
        event_data = json.loads(decrypted_json)
    except json.JSONDecodeError as exc:
        logger.error("Decrypted data is not valid JSON: %s — data: %s",
                     exc, decrypted_json[:200])
        return {"code": 1, "msg": "invalid decrypted JSON"}

    logger.info("Decrypted event: type=%s, schema=%s, header=%s",
                event_data.get("type", "unknown"),
                event_data.get("schema", ""),
                {k: v for k, v in (event_data.get("header") or {}).items()
                 if k in ("event_id", "event_type", "create_time")})

    # Step 3: Handle URL verification (shouldn't reach here, but just in case)
    if event_data.get("type") == "url_verification":
        challenge = event_data.get("challenge", "")
        return {"challenge": challenge}

    # Step 4: Extract the event payload
    event = event_data.get("event")
    if not event:
        logger.error("Decrypted event has no 'event' field. Keys: %s",
                     list(event_data.keys()))
        return {"code": 1, "msg": "no event field"}

    # Step 5: Extract message info
    message = event.get("message")
    if not message:
        logger.warning("Event has no 'message' field, might be a non-message event. "
                       "event keys: %s", list(event.keys()))
        return {"code": 0, "msg": "non-message event, ignored"}

    msg_type = message.get("message_type", "")
    message_id = message.get("message_id", "")

    if not message_id:
        logger.error("Message has no message_id")
        return {"code": 1, "msg": "no message_id"}

    # Extract sender open_id for multi-user isolation (V2.0)
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {})
    sender_open_id = sender_id.get("open_id", "")

    logger.info("Dispatching message: type=%s, id=%s, sender=%s", msg_type, message_id, sender_open_id[:8] if sender_open_id else "unknown")

    # Step 6: Dedup check
    if _dedup.is_duplicate(message_id):
        logger.info("Duplicate message_id %s, skipping", message_id)
        return {"code": 0, "msg": "duplicate, skipped"}

    # Step 7: Parse content
    content_str = message.get("content", "")
    try:
        content = json.loads(content_str) if isinstance(content_str, str) else (content_str or {})
    except (json.JSONDecodeError, TypeError):
        content = {}

    # Step 8: Dispatch to async handler (fire-and-forget with reference tracking)
    try:
        task = asyncio.create_task(dispatch_message(msg_type, content, message_id, sender_open_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception as exc:
        logger.exception("Failed to create dispatch task: %s", exc)

    return {"code": 0, "msg": "ok"}


# ======================================================================
# Webhook endpoint
# ======================================================================

@router.post("/webhook/feishu")
async def feishu_webhook(request: Request) -> Response:
    """Feishu webhook endpoint.

    Handles both plain-text events (url_verification) and encrypted events.
    Decrypts and dispatches events manually instead of using the SDK's
    EventDispatcherHandler, which has compatibility issues with certain
    lark-oapi versions.
    """
    body = await request.body()

    # Log request details for debugging
    logger.info(
        "Webhook request: content_type=%s, body_len=%d",
        request.headers.get("content-type", "unknown"),
        len(body),
    )

    # Parse the body JSON
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Webhook body is not valid JSON: %s",
                       body[:100] if body else "empty")
        return Response(
            content=json.dumps({"code": 1, "msg": "invalid JSON"}),
            status_code=200,
            media_type="application/json",
        )

    request_type = data.get("type", "")
    logger.info("Webhook request type: %s", request_type)

    # ── Case 1: URL verification challenge (plain text, not encrypted) ──
    if request_type == "url_verification":
        challenge = data.get("challenge", "")
        logger.info("URL verification challenge received, responding")
        return Response(
            content=json.dumps({"challenge": challenge}),
            status_code=200,
            media_type="application/json",
        )

    # ── Case 2: Encrypted event ──
    if "encrypt" in data:
        encrypted = data["encrypt"]
        logger.info("Encrypted event received, encrypt length=%d", len(encrypted))

        try:
            result = await _dispatch_encrypted_event(encrypted)
            return Response(
                content=json.dumps(result),
                status_code=200,
                media_type="application/json",
            )
        except Exception as exc:
            logger.exception("Failed to process encrypted event: %s", exc)
            return Response(
                content=json.dumps({"code": 0, "msg": "error handled"}),
                status_code=200,
                media_type="application/json",
            )

    # ── Case 3: Plain-text event (no encryption) ──
    # Some events may not be encrypted if encrypt_key is not configured
    if "event" in data:
        event = data.get("event", {})
        message = event.get("message", {})
        msg_type = message.get("message_type", "")
        message_id = message.get("message_id", "")

        # Extract sender open_id for multi-user isolation (V2.0)
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        sender_open_id = sender_id.get("open_id", "")

        if message_id:
            logger.info("Plain-text event: type=%s, id=%s", msg_type, message_id)
            if not _dedup.is_duplicate(message_id):
                content_str = message.get("content", "")
                try:
                    content = json.loads(content_str) if isinstance(content_str, str) else {}
                except (json.JSONDecodeError, TypeError):
                    content = {}
                try:
                    task = asyncio.create_task(dispatch_message(msg_type, content, message_id, sender_open_id))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                except Exception as exc:
                    logger.exception("Failed to dispatch plain-text event: %s", exc)

        return Response(
            content=json.dumps({"code": 0, "msg": "ok"}),
            status_code=200,
            media_type="application/json",
        )

    # ── Unknown format ──
    logger.warning("Unknown webhook format: keys=%s", list(data.keys()))
    return Response(
        content=json.dumps({"code": 0, "msg": "unknown event type"}),
        status_code=200,
        media_type="application/json",
    )
