"""Feishu message handlers: shared logic for webhook and WebSocket modes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from app.database import Neo4jDatabase
from app.feishu.cards import (
    build_complete_card,
    build_error_card,
    build_help_card,
    build_query_card,
    build_stats_card,
)
from app.feishu.client import FeishuClient
from app.ingest.pipeline import IngestPipeline
from app.models import (
    GraphStats,
    IngestRequest,
    IngestOptions,
    InputFormat,
    QueryRequest,
    SocialPlatform,
)

if TYPE_CHECKING:
    from app.auth.user_store import UserStore
    from app.services.ingest_tracker import IngestTracker

# Lazy imports for social services (to avoid hard dependency)
try:
    from app.services.social_fetcher import SocialFetcher, detect_social_url
    SOCIAL_FETCHER_AVAILABLE = True
except ImportError:
    SOCIAL_FETCHER_AVAILABLE = False
    detect_social_url = None  # type: ignore
    SocialFetcher = None  # type: ignore

try:
    from app.ingest.ocr import ImageOCRExtractor
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    ImageOCRExtractor = None  # type: ignore

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
_tracker: "IngestTracker | None" = None
_user_store: "UserStore | None" = None

# Social media services (set by init_social_services)
_social_fetcher: Any = None  # SocialFetcher | None
_ocr: Any = None             # ImageOCRExtractor | None


def init_handlers(
    ingest_pipeline: IngestPipeline,
    feishu_client: FeishuClient,
    query_pipeline: Any,
    tracker: "IngestTracker | None" = None,
    user_store: "UserStore | None" = None,
) -> None:
    """Initialize handler dependencies.

    V1.2: tracker enables IngestRecord creation for feishu-channel ingests.
    V2.0: user_store enables Feishu open_id → kb user_id mapping for multi-user isolation.
    """
    global _pipeline, _feishu_client, _query_pipeline, _tracker, _user_store
    _pipeline = ingest_pipeline
    _feishu_client = feishu_client
    _query_pipeline = query_pipeline
    _tracker = tracker
    _user_store = user_store


def init_social_services(
    social_fetcher: Any = None,
    ocr: Any = None,
) -> None:
    """Initialize social media fetching + OCR services (optional)."""
    global _social_fetcher, _ocr
    _social_fetcher = social_fetcher
    _ocr = ocr


# ======================================================================
# Message Dispatch
# ======================================================================

async def dispatch_message(
    msg_type: str, content: dict, message_id: str, sender_open_id: str = ""
) -> None:
    """Route message to appropriate handler based on type.

    V2.0: Resolves Feishu open_id → kb user_id and sets Neo4j context for
    per-user data isolation before dispatching to handlers.
    """
    try:
        # Resolve Feishu sender to kb user and set Neo4j user context
        await _resolve_sender_context(sender_open_id)

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


async def _resolve_sender_context(sender_open_id: str) -> None:
    """Resolve Feishu open_id to kb user_id and set Neo4j ContextVar.

    If user_store is available and open_id is provided, looks up or creates
    the user mapping. Falls back to default user_id for backward compatibility.
    """
    from app.config import get_settings

    if sender_open_id and _user_store:
        try:
            user = await _user_store.get_or_create_feishu_user(sender_open_id)
            if user and user.get("id"):
                Neo4jDatabase.set_current_user(user["id"])
                logger.debug(
                    "Feishu user resolved: open_id=%s... -> user_id=%s",
                    sender_open_id[:8], user["id"],
                )
                return
        except Exception as exc:
            logger.warning("Failed to resolve Feishu user %s: %s", sender_open_id[:8], exc)

    # Fallback: use default user_id
    settings = get_settings()
    Neo4jDatabase.set_current_user(settings.default_user_id)


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
    """Handle rich text: extract plain text and links.

    If a social media URL is detected (小红书/微博), route to the
    social content fetcher + OCR pipeline. Otherwise, treat as normal text.
    """
    text_parts = []
    for block in content.get("content", []):
        for element in block:
            if element.get("tag") == "text":
                text_parts.append(element.get("text", ""))
            elif element.get("tag") == "a":
                text_parts.append(element.get("href", ""))

    combined = " ".join(text_parts).strip()
    if not combined:
        return

    # Check for social media URLs
    if _social_fetcher and SOCIAL_FETCHER_AVAILABLE and detect_social_url:
        platform, social_url = detect_social_url(combined)
        if platform:
            await _handle_social_url(social_url, platform, message_id)
            return

    # Default: treat as normal text ingest
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
        records = await _pipeline.db.execute_read_for_user(
            """
            MATCH (n) WHERE (n:Entity OR n:Concept) AND n.user_id = $_user_id
            WITH count(n) AS node_count
            OPTIONAL MATCH (a)-[r]->(b) WHERE a.user_id = $_user_id AND b.user_id = $_user_id
            WITH node_count, count(r) AS edge_count
            OPTIONAL MATCH (a)-[r:IMPLICIT]->(b) WHERE a.user_id = $_user_id AND b.user_id = $_user_id
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
        results = await _pipeline.db.search_entities(keyword, limit=10)
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
        records = await _pipeline.db.execute_read_for_user(
            """
            MATCH (n) WHERE (n:Entity OR n:Concept) AND n.user_id = $_user_id
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
    """Background task: run pipeline and send complete card as new reply.

    V1.2: Creates IngestRecord for feishu-channel tracking and records token usage.
    """
    if not _pipeline or not _feishu_client:
        return

    # Generate deterministic source_id and content_hash for dedup
    source_id = f"feishu-{message_id}"
    text_content = source[:10000] if len(source) > 10000 else source
    content_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()

    # Create/check IngestRecord
    if _tracker:
        record = await _tracker.record_attempt(
            source_id=source_id,
            source_type="feishu",
            content_hash=content_hash,
        )
        if record.get("exists") and record.get("status") in ("completed", "skipped"):
            await _feishu_client.reply_text(message_id, "该消息已经处理过，跳过。")
            return
        if not record.get("exists") or record.get("status") != "processing":
            await _tracker.mark_processing(source_id)

    try:
        request = IngestRequest(
            source=source,
            options={"format": format, "channel": "feishu",
                     "file_name": file_name, "file_mime": file_mime},
        )
        result = await _pipeline.run(request)

        # Track completion with token usage
        if _tracker:
            await _tracker.mark_completed(
                source_id, kb_task_id=result.task_id,
                token_usage=result.token_usage,
            )

        # Send complete card as a new reply message
        complete_card = build_complete_card(result)
        await _feishu_client.reply_message(message_id, complete_card)

    except Exception as exc:
        logger.exception("Ingest pipeline failed for task %s", task_id)
        if _tracker:
            await _tracker.mark_failed(source_id, str(exc))
        # Friendly error for Pydantic validation errors (e.g. file too large)
        err_msg = str(exc)
        if "string_too_long" in err_msg or "String should have at most" in err_msg:
            err_msg = "文件过大，超过了系统处理上限（最大 20MB）。请压缩后再试。"
        elif "PDF 提取" in err_msg:
            # Keep PDF extraction errors as-is, they're already user-friendly
            pass
        await send_error(message_id, err_msg)


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


# ======================================================================
# Social Media URL Handling
# ======================================================================

async def _handle_social_url(
    url: str, platform: SocialPlatform, message_id: str
) -> None:
    """Handle a social media URL: fetch content → OCR images → ingest pipeline.

    The flow:
        1. Reply immediately with "正在提取…" (webhook < 3s requirement)
        2. Background task: fetch → download images → OCR → markdown → pipeline
        3. Send complete card on finish (or error card on failure)
    """
    if not _feishu_client or not _pipeline:
        return

    platform_cn = "小红书" if platform == SocialPlatform.XIAOHONGSHU else "微博"
    await _feishu_client.reply_text(message_id, f"🔍 正在提取{platform_cn}内容...")

    task = asyncio.create_task(
        _run_social_fetch_and_ingest(url, platform, message_id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_social_fetch_and_ingest(
    url: str, platform: SocialPlatform, message_id: str
) -> None:
    """Background task: full social media → knowledge pipeline.

    V1.2: Creates IngestRecord for feishu-channel tracking and records token usage.
    """
    if not _pipeline or not _feishu_client or not _social_fetcher:
        return

    platform_cn = "小红书" if platform == SocialPlatform.XIAOHONGSHU else "微博"
    t_start = time.time()

    # Generate deterministic source_id for dedup
    source_id = f"feishu-social-{hashlib.sha256(url.encode()).hexdigest()[:16]}"

    try:
        # --- Step 0a: Fetch content via Playwright/httpx ---
        content = await _social_fetcher.fetch(url, platform)

        if content.fetch_status.value == "failed":
            if _tracker:
                await _tracker.record_attempt(
                    source_id=source_id, source_type="feishu",
                    content_hash=hashlib.sha256(url.encode()).hexdigest(),
                )
                await _tracker.mark_failed(source_id, content.error or "fetch failed")
            await _feishu_client.reply_text(
                message_id,
                f"❌ {platform_cn}内容提取失败: {content.error}",
            )
            return

        # --- Step 0b: Download images + OCR ---
        ocr_engine = "none"
        if _ocr and content.images:
            images_with_data = [
                img for img in content.images if img.base64
            ]
            if images_with_data:
                await _feishu_client.reply_text(
                    message_id,
                    f"📷 正在识别 {len(images_with_data)} 张图片中的文字...",
                )
                b64_list = [img.base64 for img in images_with_data]
                ocr_results = await _ocr.extract_batch(b64_list)
                for i, result in enumerate(ocr_results):
                    images_with_data[i].ocr_text = result.text
                    images_with_data[i].ocr_engine = result.engine
                ocr_engine = ocr_results[0].engine if ocr_results else "none"

        # --- Step 0c: Convert to Markdown ---
        markdown = content.to_ingest_markdown()
        content_hash = hashlib.sha256(markdown[:10000].encode("utf-8")).hexdigest()

        # Create IngestRecord
        if _tracker:
            record = await _tracker.record_attempt(
                source_id=source_id, source_type="feishu",
                content_hash=content_hash,
            )
            if record.get("exists") and record.get("status") in ("completed", "skipped"):
                await _feishu_client.reply_text(message_id, "该内容已经处理过，跳过。")
                return
            await _tracker.mark_processing(source_id)

        # --- Step 1-4: Ingest pipeline (same as any text input) ---
        request = IngestRequest(
            source=markdown,
            options=IngestOptions(
                format=InputFormat.MARKDOWN,
                channel="feishu",
                tags=content.tags,
            ),
        )
        result = await _pipeline.run(request)

        # Track completion with token usage
        if _tracker:
            await _tracker.mark_completed(
                source_id, kb_task_id=result.task_id,
                token_usage=result.token_usage,
            )

        # --- Build and send complete card ---
        complete_card = build_complete_card(result)
        await _feishu_client.reply_message(message_id, complete_card)

        # Log timing
        elapsed = time.time() - t_start
        image_count = len(content.images)
        ocr_count = sum(1 for img in content.images if img.ocr_text)
        logger.info(
            "Social ingest complete: platform=%s url=%s elapsed=%.1fs "
            "images=%d ocr=%d engine=%s",
            platform.value, url, elapsed, image_count, ocr_count, ocr_engine,
        )

    except Exception as exc:
        logger.exception("Social fetch+ingest failed for %s", url)
        if _tracker:
            await _tracker.mark_failed(source_id, str(exc))
        await send_error(message_id, f"{platform_cn}知识处理失败: {exc}")
