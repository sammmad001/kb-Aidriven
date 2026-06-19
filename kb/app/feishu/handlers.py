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
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from app.database import Neo4jDatabase
from app.feishu.cards import (
    build_bind_error_card,
    build_bind_success_card,
    build_complete_card,
    build_error_card,
    build_help_card,
    build_query_card,
    build_recent_card,
    build_register_error_card,
    build_register_success_card,
    build_stats_card,
    build_unbind_card,
    build_unbound_prompt_card,
    build_whoami_card,
)
from app.feishu.client import FeishuClient
from app.feishu.context import ConversationContext
from app.feishu.intent import IntentDetector
from app.feishu.research_cards import build_research_result_card, build_research_unavailable_card
from app.ingest.pipeline import IngestPipeline
from app.models import (
    GraphStats,
    IngestRequest,
    IngestOptions,
    InputFormat,
    QueryRequest,
    SocialPlatform,
    TaskStatusEnum,
)

if TYPE_CHECKING:
    from app.auth.user_store import UserStore
    from app.services.ingest_tracker import IngestTracker
    from app.services.miromind_client import MiroMindClient
    from app.services.research_tracker import ResearchTracker

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

# Intent detection + conversation context (set by init_intent_context)
_intent_detector: IntentDetector | None = None
_context: ConversationContext | None = None

# MiroMind deep research (set by init_research_service)
_miromind_client: "MiroMindClient | None" = None
_research_tracker: "ResearchTracker | None" = None

# Current Feishu sender open_id for this async context (set by dispatch_message)
_current_open_id: ContextVar[str] = ContextVar("current_open_id", default="")


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


def init_intent_context(
    intent_detector: IntentDetector,
    context: ConversationContext,
) -> None:
    """Initialize intent detection and conversation context services."""
    global _intent_detector, _context
    _intent_detector = intent_detector
    _context = context


def init_research_service(
    miromind_client: "MiroMindClient",
    research_tracker: "ResearchTracker | None" = None,
) -> None:
    """Initialize MiroMind deep research service + persistence tracker."""
    global _miromind_client, _research_tracker
    _miromind_client = miromind_client
    _research_tracker = research_tracker


# ======================================================================
# Message Dispatch
# ======================================================================

async def dispatch_message(
    msg_type: str, content: dict, message_id: str, sender_open_id: str = ""
) -> None:
    """Route message to appropriate handler based on type.

    V2.0: Resolves Feishu open_id → kb user_id and sets Neo4j context for
    per-user data isolation before dispatching to handlers.
    V2.1: Unbound users are intercepted — only /bind, /help, /whoami allowed.
    """
    try:
        # Store sender open_id for this async context (used by handle_bind etc.)
        _current_open_id.set(sender_open_id)

        # Check binding status (does NOT auto-create)
        bound = False
        if sender_open_id and _user_store:
            binding = await _user_store.get_feishu_binding_status(sender_open_id)
            if binding["bound"]:
                Neo4jDatabase.set_current_user(binding["user_id"])
                bound = True

        if not bound:
            # Unbound user: only allow bind/help/whoami commands
            text = content.get("text", "").strip() if msg_type == "text" else ""
            cmd, cmd_args = parse_command(text) if text else ("input", "")

            if cmd in ("register", "bind", "help", "whoami"):
                # Route to the specific command handler
                if cmd == "register":
                    await handle_register(cmd_args, message_id)
                elif cmd == "bind":
                    await handle_bind(cmd_args, message_id)
                elif cmd == "help":
                    await handle_help(message_id)
                elif cmd == "whoami":
                    await handle_whoami(message_id)
            else:
                # Intercept everything else with a binding prompt
                if _feishu_client:
                    await _feishu_client.reply_message(
                        message_id, build_unbound_prompt_card(),
                    )
            return

        # Bound user: normal dispatch
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


async def _resolve_sender_context(sender_open_id: str) -> dict | None:
    """Query binding status for *sender_open_id* (does NOT auto-create).

    Returns ``{"bound": bool, "user_id": str|None, "username": str|None}``
    or ``None`` if user_store / open_id is unavailable.
    """
    if sender_open_id and _user_store:
        return await _user_store.get_feishu_binding_status(sender_open_id)
    return None


# ======================================================================
# Message Type Handlers
# ======================================================================

async def handle_text(content: dict, message_id: str) -> None:
    """Handle text message: check for commands or treat as knowledge input.

    V2.2: Integrates intent detection — questions are auto-routed to query
    without requiring /q prefix. Conversation context enables follow-up
    entity enrichment.
    """
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
    elif cmd == "research":
        await handle_research(args, message_id)
    elif cmd == "research_status":
        await handle_research_status(message_id)
    elif cmd == "recent":
        await handle_recent(message_id, args)
    elif cmd == "end":
        await handle_end(message_id)
    elif cmd == "help":
        await handle_help(message_id)
    elif cmd == "register":
        await handle_register(args, message_id)
    elif cmd == "bind":
        await handle_bind(args, message_id)
    elif cmd == "unbind":
        await handle_unbind(message_id)
    elif cmd == "whoami":
        await handle_whoami(message_id)
    else:
        # Social media URL detection (小红书/微博): route to social pipeline
        # before intent detection — covers plain-text shares from mobile apps
        if _social_fetcher and SOCIAL_FETCHER_AVAILABLE and detect_social_url:
            platform, social_url = detect_social_url(text)
            if platform:
                await _handle_social_url(social_url, platform, message_id)
                return

        # Fast research keyword pre-check (before intent detection)
        # Catches obvious research requests so they skip the intent pipeline
        if _miromind_client and _miromind_client.is_configured:
            _research_kw = (
                "深度研究", "深入研究", "帮我研究", "研究一下",
                "深度分析", "详细分析", "全面分析",
                "用miromind", "调用miromind",
            )
            if any(kw in text.lower() for kw in _research_kw):
                await handle_research(text, message_id)
                return

        # Intent auto-detection: classify as query, input, or research
        user_id = Neo4jDatabase.get_current_user_id_or_default()
        has_ctx = _context.has_active_context(user_id) if _context else False
        if _intent_detector:
            intent = await _intent_detector.detect(text, has_recent_query=has_ctx)
        else:
            intent = "input"  # safe default when detector unavailable
        if intent == "research":
            await handle_research(text, message_id)
        elif intent == "query":
            await handle_query(text, message_id, user_id=user_id)
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

async def handle_query(question: str, message_id: str, user_id: str | None = None) -> None:
    """Handle /q command or auto-detected query: query the knowledge base.

    V2.2: Integrates conversation context — follow-up questions are enriched
    with entity context and query history is passed to the LLM.
    """
    if not _query_pipeline or not _feishu_client:
        return

    # Resolve user_id for context isolation
    if user_id is None:
        user_id = Neo4jDatabase.get_current_user_id_or_default()

    # Enrich follow-up question with entity context if available
    enriched_question = question
    context_history: list[dict[str, Any]] = []
    if _context:
        enriched_question = _context.enrich_followup(user_id, question)
        context_history = _context.get_context_history(user_id)

    await _feishu_client.reply_text(message_id, "正在查询...")

    try:
        result = await _query_pipeline.run(QueryRequest(
            question=enriched_question,
            context_history=context_history,
        ))

        # Record this turn in conversation context
        if _context:
            _context.add_turn(
                user_id=user_id,
                question=question,  # original question, not enriched
                answer=result.answer,
            )

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


async def handle_recent(message_id: str, args: str = "") -> None:
    """Handle /recent command: show recently updated nodes with optional filters.

    Supported args:
        /recent 20            — limit to 20 records
        /recent entity        — filter by label (Entity/Concept/Comparison)
        /recent 7d            — filter by time window (e.g. 1d, 7d, 30d)
        /recent entity 7d 20  — combined filters
    """
    if not _pipeline or not _feishu_client:
        return

    # Parse args
    limit = 10
    label_filter: str | None = None
    days_filter: int | None = None

    for token in args.split():
        if token.isdigit():
            limit = min(int(token), 50)
        elif re.match(r'^(\d+)d$', token, re.IGNORECASE):
            days_filter = int(re.match(r'^(\d+)d$', token, re.IGNORECASE).group(1))
        elif token.lower() in ("entity", "concept", "comparison"):
            label_filter = token.capitalize()

    # Build Cypher query with optional filters
    where_clauses = ["(n:Entity OR n:Concept OR n:Comparison)", "n.user_id = $_user_id"]
    if days_filter is not None:
        where_clauses.append(f"n.updated_at >= datetime() - duration({{days: {days_filter}}})")
    where_clause = " AND ".join(where_clauses)

    label_clause = ""
    if label_filter:
        label_clause = f" AND '{label_filter}' IN labels(n)"

    cypher = f"""
        MATCH (n) WHERE {where_clause}{label_clause}
        RETURN n.name AS name, n.summary AS summary,
               n.updated_at AS updated, labels(n) AS labels
        ORDER BY n.updated_at DESC LIMIT {limit}
    """

    try:
        records = await _pipeline.db.execute_read_for_user(cypher)
        card = build_recent_card(records)
        await _feishu_client.reply_message(message_id, card)
    except Exception as exc:
        await send_error(message_id, f"查询失败: {exc}")


async def handle_help(message_id: str) -> None:
    """Handle /help command."""
    if _feishu_client:
        card = build_help_card()
        await _feishu_client.reply_message(message_id, card)


async def handle_end(message_id: str) -> None:
    """Handle /end command: clear conversation context for the current user."""
    if not _feishu_client:
        return
    user_id = Neo4jDatabase.get_current_user_id_or_default()
    if _context:
        _context.clear(user_id)
    await _feishu_client.reply_text(message_id, "对话上下文已清空。")


# ======================================================================
# Account Binding Handlers (V2.1)
# ======================================================================

async def handle_register(args: str, message_id: str) -> None:
    """Handle /register command: create a new account + auto-bind.

    Usage: /register <username> <password>
    Validates username (3-50 chars, [a-zA-Z0-9_-]) and password (≥6 chars)
    before delegating to UserStore.register_feishu_user.
    """
    if not _feishu_client or not _user_store:
        return

    open_id = _current_open_id.get("")
    if not open_id:
        logger.warning("handle_register called without sender open_id")
        return

    parts = args.split()
    if len(parts) < 2:
        await _feishu_client.reply_message(
            message_id, build_register_error_card("invalid_format"),
        )
        return

    username, password = parts[0], parts[1]

    # Validate username format: 3-50 chars, [a-zA-Z0-9_-]
    if not re.match(r'^[a-zA-Z0-9_\-]{3,50}$', username):
        await _feishu_client.reply_message(
            message_id, build_register_error_card("invalid_format"),
        )
        return

    # Validate password length ≥ 6
    if len(password) < 6:
        await _feishu_client.reply_message(
            message_id, build_register_error_card("invalid_password"),
        )
        return

    await _feishu_client.reply_text(message_id, "正在注册账户...")

    db = _pipeline.db if _pipeline else None
    result = await _user_store.register_feishu_user(
        open_id, username, password, db=db,
    )

    if result["success"]:
        Neo4jDatabase.set_current_user(result["user_id"])
        migrated = result.get("migrated_nodes", 0)
        await _feishu_client.reply_message(
            message_id, build_register_success_card(username, migrated),
        )
    else:
        error = result.get("error", "unknown")
        await _feishu_client.reply_message(
            message_id, build_register_error_card(error),
        )


async def handle_bind(args: str, message_id: str) -> None:
    """Handle /bind command: verify credentials + bind + migrate data.

    Usage: /bind <username> <password>
    """
    if not _feishu_client or not _user_store:
        return

    open_id = _current_open_id.get("")
    if not open_id:
        logger.warning("handle_bind called without sender open_id")
        return

    parts = args.split()
    if len(parts) < 2:
        await _feishu_client.reply_message(
            message_id, build_bind_error_card("格式错误"),
        )
        return

    username, password = parts[0], parts[1]

    await _feishu_client.reply_text(message_id, "正在绑定账户...")

    # Pass Neo4j db instance for data migration if available
    db = _pipeline.db if _pipeline else None
    result = await _user_store.bind_feishu_user(
        open_id, username, password, db=db,
    )

    if result["success"]:
        Neo4jDatabase.set_current_user(result["user_id"])
        migrated = result.get("migrated_nodes", 0)
        await _feishu_client.reply_message(
            message_id, build_bind_success_card(username, migrated),
        )
    else:
        error = result.get("error", "unknown")
        if error == "invalid_credentials":
            msg = "用户名或密码错误"
        else:
            msg = f"绑定失败: {error}"
        await _feishu_client.reply_message(
            message_id, build_bind_error_card(msg),
        )


async def handle_unbind(message_id: str) -> None:
    """Handle /unbind command: remove Feishu → Web account binding."""
    if not _feishu_client or not _user_store:
        return

    open_id = _current_open_id.get("")
    if open_id:
        await _user_store.unbind_feishu_user(open_id)

    await _feishu_client.reply_message(message_id, build_unbind_card())


async def handle_whoami(message_id: str) -> None:
    """Handle /whoami command: show current binding status."""
    if not _feishu_client or not _user_store:
        return

    open_id = _current_open_id.get("")
    if not open_id:
        return

    binding = await _user_store.get_feishu_binding_status(open_id)
    await _feishu_client.reply_message(message_id, build_whoami_card(binding))


async def handle_research(question: str, message_id: str) -> None:
    """Handle /research command: trigger MiroMind deep research.

    Sends an instant confirmation, then runs the research in a background task.
    """
    if not _feishu_client:
        return

    # Check if MiroMind is configured
    if not _miromind_client or not _miromind_client.is_configured:
        card = build_research_unavailable_card()
        await _feishu_client.reply_message(message_id, card)
        return

    # Create persistent task record (survives server restart)
    task_id = ""
    if _research_tracker:
        try:
            task_id = await _research_tracker.create_task(question, message_id)
        except Exception as exc:
            logger.warning("Failed to create research task record: %s", exc)

    # Instant acknowledgment
    await _feishu_client.reply_text(
        message_id,
        f"🔬 正在调用 MiroMind 进行深度研究，请耐心等待...\n"
        f"📋 任务ID: {task_id[:8] if task_id else 'N/A'}\n"
        f"💡 发送 /rs 随时查看进度",
    )

    # Run research in background
    task = asyncio.create_task(
        _run_research_and_send(question, message_id, task_id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_research_and_send(question: str, message_id: str, task_id: str = "") -> None:
    """Background task: run MiroMind research → ingest into KB → send result card.

    V1.2.6: Full lifecycle tracking via ResearchTracker. Each phase is persisted
    so users can query status and interrupted tasks are recoverable on restart.
    """
    if not _feishu_client or not _miromind_client:
        return

    # Phase 1: Mark as running
    if task_id and _research_tracker:
        try:
            await _research_tracker.mark_running(task_id)
        except Exception as exc:
            logger.warning("Failed to mark research task running: %s", exc)

    # Phase 2: Call MiroMind API (non-streaming)
    result = await _miromind_client.research(question)

    if result.status == "error":
        # Persist failure
        if task_id and _research_tracker:
            try:
                await _research_tracker.mark_failed(task_id, result.error or "Unknown error")
            except Exception as exc:
                logger.warning("Failed to mark research task failed: %s", exc)
        card = build_research_result_card(question, result)
        await _feishu_client.reply_message(message_id, card)
        return

    # Phase 3: Auto-ingest into knowledge base
    ingest_summary = ""
    settings = None
    try:
        from app.config import get_settings
        settings = get_settings()
    except Exception:
        pass

    if settings and settings.miromind_auto_ingest and _pipeline and result.content:
        try:
            from app.adapters.miromind import MiroMindAdapter

            adapter = MiroMindAdapter(min_tokens=settings.miromind_min_tokens)
            payload = result.to_miromind_payload()
            extracted = await adapter.extract(payload)
            is_valid, reason = await adapter.validate(extracted)

            if is_valid:
                source, options = await adapter.transform(extracted)
                ingest_result = await _pipeline.run(IngestRequest(source=source, options=options.__dict__))
                gr = ingest_result.graph_result
                n_new = len(gr.nodes_created) if gr else 0
                n_upd = len(gr.nodes_updated) if gr else 0
                ingest_summary = f"新增 {n_new} 节点，更新 {n_upd} 节点"
            else:
                ingest_summary = f"跳过入库（{reason}）"
        except Exception as exc:
            logger.warning("Research auto-ingest failed: %s", exc)
            ingest_summary = f"入库失败: {exc}"

    # Phase 4: Persist completion + send result card
    if task_id and _research_tracker:
        try:
            await _research_tracker.mark_completed(
                task_id,
                content=result.content,
                model=result.model,
                total_tokens=result.total_tokens,
                duration_ms=result.duration_ms,
                ingest_summary=ingest_summary,
            )
        except Exception as exc:
            logger.warning("Failed to mark research task completed: %s", exc)

    card = build_research_result_card(question, result, ingest_summary)
    await _feishu_client.reply_message(message_id, card)


# ======================================================================
# Research Status Query
# ======================================================================

async def handle_research_status(message_id: str) -> None:
    """Handle /research-status (or /rs): show recent research tasks."""
    if not _feishu_client:
        return

    if not _research_tracker:
        await _feishu_client.reply_text(
            message_id,
            "⚠️ 研究任务追踪未启用（数据库未连接）",
        )
        return

    try:
        tasks = await _research_tracker.get_recent_tasks(limit=5)
    except Exception as exc:
        logger.warning("Failed to query research tasks: %s", exc)
        await _feishu_client.reply_text(message_id, "⚠️ 查询研究任务失败，请稍后重试")
        return

    if not tasks:
        await _feishu_client.reply_text(
            message_id,
            "📋 暂无研究任务记录\n💡 发送 /research <主题> 开始一次深度研究",
        )
        return

    # Build text summary
    lines = ["📋 **最近研究任务**", ""]
    status_emoji = {
        "completed": "✅",
        "running": "🔄",
        "pending": "⏳",
        "failed": "❌",
    }
    for t in tasks:
        emoji = status_emoji.get(t.get("status", ""), "❓")
        q = (t.get("question") or "")[:40]
        tid = (t.get("task_id") or "")[:8]
        status = t.get("status", "unknown")
        line = f"{emoji} `{tid}` {q}"
        if status == "completed":
            tokens = t.get("result_tokens", 0) or 0
            dur = t.get("duration_ms", 0) or 0
            ingest = t.get("ingest_summary", "")
            extra = f" · {tokens} tokens · {dur}ms"
            if ingest:
                extra += f" · {ingest}"
            line += extra
        elif status == "failed":
            err = (t.get("error") or "")[:60]
            line += f"\n   └ 错误: {err}"
        elif status == "running":
            line += " · 进行中..."
        lines.append(line)

    lines.append("")
    lines.append("💡 使用 /research <主题> 开始新的研究")
    await _feishu_client.reply_text(message_id, "\n".join(lines))


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

        # Check pipeline status — FAILED should not be shown as success
        if result.status == TaskStatusEnum.FAILED:
            if _tracker:
                await _tracker.mark_failed(source_id, result.error or "pipeline failed")
            err_msg = result.error or "处理失败"
            # PDF 提取错误已经是友好提示，保留原文
            if "PDF 提取" not in err_msg and "PDF 内容提取" not in err_msg:
                err_msg = f"知识处理失败: {err_msg}"
            await send_error(message_id, err_msg)
            return

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
    Commands: /q (query), /stats, /search, /recent, /help, /end
    """
    m = re.match(r'^/(?:q|query)\s+(.+)', text, re.IGNORECASE)
    if m:
        return "query", m.group(1).strip()

    if re.match(r'^/stats', text, re.IGNORECASE):
        return "stats", ""

    m = re.match(r'^/search\s+(.+)', text, re.IGNORECASE)
    if m:
        return "search", m.group(1).strip()

    m = re.match(r'^/research\s+(.+)', text, re.IGNORECASE)
    if m:
        return "research", m.group(1).strip()

    # /research-status or /rs — query recent research task status
    if re.match(r'^/(?:research-status|rs)(?:\s.*)?$', text, re.IGNORECASE):
        return "research_status", ""

    m = re.match(r'^/recent\s*(.*)', text, re.IGNORECASE)
    if m:
        return "recent", m.group(1).strip()

    if re.match(r'^/help', text, re.IGNORECASE):
        return "help", ""

    if re.match(r'^/end', text, re.IGNORECASE):
        return "end", ""

    # Account registration + binding commands (V2.1)
    m = re.match(r'^/register\s+(\S+)\s+(\S+)', text, re.IGNORECASE)
    if m:
        return "register", f"{m.group(1)} {m.group(2)}"

    m = re.match(r'^/bind\s+(\S+)\s+(\S+)', text, re.IGNORECASE)
    if m:
        return "bind", f"{m.group(1)} {m.group(2)}"

    if re.match(r'^/unbind', text, re.IGNORECASE):
        return "unbind", ""

    if re.match(r'^/whoami', text, re.IGNORECASE):
        return "whoami", ""

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
