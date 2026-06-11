"""Feishu message handlers: shared logic for webhook and WebSocket modes."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

from app.feishu.cards import (
    build_complete_card,
    build_error_card,
    build_help_card,
    build_query_card,
    build_stats_card,
)
from app.feishu.client import FeishuClient
from app.ingest.pipeline import IngestPipeline
from app.models import GraphStats, IngestRequest, InputFormat, QueryRequest

logger = logging.getLogger(__name__)

# Maximum file size for uploads (20 MB)
MAX_FILE_SIZE = 20 * 1024 * 1024


# ======================================================================
# Message Deduplication
# ======================================================================

class _MessageDeduplicator:
    """TTL-based message_id deduplication to prevent webhook retry duplicates."""

    def __init__(self, ttl_seconds: int = 300, max_size: int = 10000) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl_seconds
        self._max_size = max_size

    def is_duplicate(self, message_id: str) -> bool:
        """Return True if message_id was already processed within TTL window."""
        now = time.time()
        # Evict expired entries
        while self._seen and next(iter(self._seen.values())) < now - self._ttl:
            self._seen.popitem(last=False)
        # Evict oldest if over capacity
        while len(self._seen) >= self._max_size:
            self._seen.popitem(last=False)
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False


_dedup = _MessageDeduplicator()

# Prevent GC of background tasks (RES-01 fix)
_background_tasks: set[asyncio.Task] = set()

# Module-level dependencies (set by init_handlers)
_pipeline: IngestPipeline | None = None
_feishu_client: FeishuClient | None = None
_query_pipeline: Any = None


def init_handlers(
    ingest_pipeline: IngestPipeline,
    feishu_client: FeishuClient,
    query_pipeline: Any,
) -> None:
    """Initialize handler dependencies."""
    global _pipeline, _feishu_client, _query_pipeline
    _pipeline = ingest_pipeline
    _feishu_client = feishu_client
    _query_pipeline = query_pipeline


# ======================================================================
# Message Dispatch
# ======================================================================

async def dispatch_message(msg_type: str, content: dict, message_id: str) -> None:
    """Route message to appropriate handler based on type."""
    try:
        handlers = {
            "text": handle_text,
            "rich_text": handle_rich_text,
            "image": handle_image,
            "file": handle_file,
            "audio": handle_audio,
        }
        handler = handlers.get(msg_type)
        if handler:
            await handler(content, message_id)
        else:
            await send_error(message_id, f"不支持的消息类型: {msg_type}")
    except Exception as exc:
        logger.exception("Message dispatch failed")
        await send_error(message_id, f"处理失败: {exc}")


# ======================================================================
# Message Type Handlers
# ======================================================================

async def handle_text(content: dict, message_id: str) -> None:
    """Handle text message: check for commands or treat as knowledge input."""
    text = content.get("text", "").strip()
    if not text:
        return

    cmd, args = parse_command(text)

    if cmd == "query":
        await handle_query(args, message_id)
    elif cmd == "stats":
        await handle_stats(message_id)
    elif cmd == "search":
        await handle_search(args, message_id)
    elif cmd == "recent":
        await handle_recent(message_id)
    elif cmd == "help":
        await handle_help(message_id)
    else:
        await handle_ingest_text(text, message_id)


async def handle_rich_text(content: dict, message_id: str) -> None:
    """Handle rich text: extract plain text and links."""
    text_parts = []
    for block in content.get("content", []):
        for element in block:
            if element.get("tag") == "text":
                text_parts.append(element.get("text", ""))
            elif element.get("tag") == "a":
                text_parts.append(element.get("href", ""))

    combined = " ".join(text_parts).strip()
    if combined:
        await handle_ingest_text(combined, message_id)


async def handle_image(content: dict, message_id: str) -> None:
    """Handle image: download and ingest."""
    file_key = content.get("image_key", "")
    if not file_key or not _feishu_client:
        return

    try:
        image_bytes = await _feishu_client.download_resource(message_id, file_key, "image")
    except Exception as exc:
        logger.error("Image download failed: %s", exc)
        await send_error(message_id, "图片下载失败")
        return
    if len(image_bytes) > MAX_FILE_SIZE:
        await send_error(message_id, f"文件过大（上限 {MAX_FILE_SIZE // 1024 // 1024}MB）")
        return
    image_b64 = base64.b64encode(image_bytes).decode()

    await run_ingest(
        source=image_b64,
        format=InputFormat.IMAGE,
        message_id=message_id,
        file_name=f"image-{file_key[:8]}",
        file_mime="image/jpeg",
    )


async def handle_file(content: dict, message_id: str) -> None:
    """Handle file: download and ingest."""
    file_key = content.get("file_key", "")
    file_name = content.get("file_name", "uploaded-file")
    if not file_key or not _feishu_client:
        return

    try:
        file_bytes = await _feishu_client.download_resource(message_id, file_key, "file")
    except Exception as exc:
        logger.error("File download failed for %s: %s", file_name, exc)
        await send_error(message_id, f"文件下载失败: {file_name}")
        return
    if len(file_bytes) > MAX_FILE_SIZE:
        await send_error(message_id, f"文件过大（上限 {MAX_FILE_SIZE // 1024 // 1024}MB）")
        return
    file_b64 = base64.b64encode(file_bytes).decode()

    mime = "application/octet-stream"
    if file_name.endswith(".pdf"):
        mime = "application/pdf"
    elif file_name.endswith(".docx"):
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif file_name.endswith(".txt") or file_name.endswith(".md"):
        mime = "text/plain"

    await run_ingest(
        source=file_b64,
        format=InputFormat.FILE,
        message_id=message_id,
        file_name=file_name,
        file_mime=mime,
    )


async def handle_audio(content: dict, message_id: str) -> None:
    """Handle audio: download and mark for transcription."""
    file_key = content.get("file_key", "")
    if not file_key or not _feishu_client:
        return

    try:
        audio_bytes = await _feishu_client.download_resource(message_id, file_key, "audio")
    except Exception as exc:
        logger.error("Audio download failed: %s", exc)
        await send_error(message_id, "语音下载失败")
        return
    if len(audio_bytes) > MAX_FILE_SIZE:
        await send_error(message_id, f"文件过大（上限 {MAX_FILE_SIZE // 1024 // 1024}MB）")
        return
    audio_b64 = base64.b64encode(audio_bytes).decode()

    await run_ingest(
        source=audio_b64,
        format=InputFormat.AUDIO,
        message_id=message_id,
        file_name=f"audio-{file_key[:8]}",
    )


# ======================================================================
# Command Handlers
# ======================================================================

async def handle_query(question: str, message_id: str) -> None:
    """Handle /q command: query the knowledge base."""
    if not _query_pipeline or not _feishu_client:
        return

    await _feishu_client.reply_text(message_id, "正在查询...")

    try:
        result = await _query_pipeline.run(QueryRequest(question=question))
        card = build_query_card(result)
        await _feishu_client.reply_message(message_id, card)
    except Exception as exc:
        await send_error(message_id, f"查询失败: {exc}")


async def handle_stats(message_id: str) -> None:
    """Handle /stats command: show knowledge base statistics."""
    if not _pipeline or not _feishu_client:
        return

    try:
        records = await _pipeline._db.execute_read(
            """
            MATCH (n) WHERE n:Entity OR n:Concept
            WITH count(n) AS node_count
            OPTIONAL MATCH ()-[r]->() 
            WITH node_count, count(r) AS edge_count
            OPTIONAL MATCH ()-[r:IMPLICIT]->()
            WITH node_count, edge_count, count(r) AS implicit_count
            RETURN node_count, edge_count, implicit_count
            """
        )
        if records:
            r = records[0]
            stats = GraphStats(
                node_count=r.get("node_count", 0),
                edge_count=r.get("edge_count", 0),
                implicit_edge_count=r.get("implicit_count", 0),
            )
        else:
            stats = GraphStats()

        card = build_stats_card(stats)
        await _feishu_client.reply_message(message_id, card)
    except Exception as exc:
        await send_error(message_id, f"统计查询失败: {exc}")


async def handle_search(keyword: str, message_id: str) -> None:
    """Handle /search command: search knowledge nodes."""
    if not _pipeline or not _feishu_client:
        return

    try:
        results = await _pipeline._db.search_entities(keyword, limit=10)
        if results:
            items = "\n".join(f"- **{r['name']}**: {r.get('summary', '')}" for r in results)
        else:
            items = "未找到匹配的知识节点"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"🔍 搜索: {keyword}"}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": items}}],
        }
        await _feishu_client.reply_message(message_id, card)
    except Exception as exc:
        await send_error(message_id, f"搜索失败: {exc}")


async def handle_recent(message_id: str) -> None:
    """Handle /recent command: show recently updated nodes."""
    if not _pipeline or not _feishu_client:
        return

    try:
        records = await _pipeline._db.execute_read(
            """
            MATCH (n) WHERE n:Entity OR n:Concept
            RETURN n.name AS name, n.summary AS summary, n.updated_at AS updated
            ORDER BY n.updated_at DESC LIMIT 10
            """
        )
        if records:
            items = "\n".join(f"- **{r['name']}** — {r.get('summary', '')}" for r in records)
        else:
            items = "知识库暂无内容"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "📝 最近更新"}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": items}}],
        }
        await _feishu_client.reply_message(message_id, card)
    except Exception as exc:
        await send_error(message_id, f"查询失败: {exc}")


async def handle_help(message_id: str) -> None:
    """Handle /help command."""
    if _feishu_client:
        card = build_help_card()
        await _feishu_client.reply_message(message_id, card)


# ======================================================================
# Ingest
# ======================================================================

async def handle_ingest_text(text: str, message_id: str) -> None:
    """Ingest text as knowledge input."""
    await run_ingest(source=text, format=InputFormat.TEXT, message_id=message_id)


async def run_ingest(
    source: str,
    format: InputFormat,
    message_id: str,
    file_name: str | None = None,
    file_mime: str | None = None,
) -> None:
    """Run ingest: send text ack instantly, then send result card when done."""
    if not _pipeline or not _feishu_client:
        return

    # Step 1: Send instant text feedback (<1s)
    await _feishu_client.reply_text(message_id, "已收到，知识分析和推理中...")

    # Step 2: Run pipeline in background, send complete card when done
    task_id = uuid.uuid4().hex[:8]
    task = asyncio.create_task(
        _run_pipeline_and_send(
            source=source,
            format=format,
            message_id=message_id,
            task_id=task_id,
            file_name=file_name,
            file_mime=file_mime,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_pipeline_and_send(
    source: str,
    format: InputFormat,
    message_id: str,
    task_id: str,
    file_name: str | None = None,
    file_mime: str | None = None,
) -> None:
    """Background task: run pipeline and send complete card as new reply."""
    if not _pipeline or not _feishu_client:
        return

    try:
        request = IngestRequest(
            source=source,
            options={"format": format, "channel": "feishu",
                     "file_name": file_name, "file_mime": file_mime},
        )
        result = await _pipeline.run(request)

        # Send complete card as a new reply message
        complete_card = build_complete_card(result)
        await _feishu_client.reply_message(message_id, complete_card)

    except Exception as exc:
        logger.exception("Ingest pipeline failed for task %s", task_id)
        await send_error(message_id, f"任务 `{task_id}` 处理失败: {exc}")


# ======================================================================
# Helpers
# ======================================================================

def parse_command(text: str) -> tuple[str, str]:
    """Parse command prefix from text.

    Returns (command, args).
    Commands: /q (query), /stats, /search, /recent, /help
    """
    m = re.match(r'^/(?:q|query)\s+(.+)', text, re.IGNORECASE)
    if m:
        return "query", m.group(1).strip()

    if re.match(r'^/stats', text, re.IGNORECASE):
        return "stats", ""

    m = re.match(r'^/search\s+(.+)', text, re.IGNORECASE)
    if m:
        return "search", m.group(1).strip()

    if re.match(r'^/recent', text, re.IGNORECASE):
        return "recent", ""

    if re.match(r'^/help', text, re.IGNORECASE):
        return "help", ""

    return "input", text


async def send_error(message_id: str, message: str) -> None:
    """Send an error card to the user."""
    if _feishu_client:
        card = build_error_card("处理错误", message, "请稍后重试或使用 /help 查看帮助")
        try:
            await _feishu_client.reply_message(message_id, card)
        except Exception as exc:
            logger.warning("Failed to send error card for message %s: %s", message_id, exc)
