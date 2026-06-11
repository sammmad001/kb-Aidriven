"""
SSE 事件缓冲器 - 在流式传输同时缓冲事件，完成后写入数据库

变更说明（个人知识库集成）：
- 新增 session_title / session_model 参数
- 新增 _post_to_kb() 方法：流结束后异步 POST 到 KB 入库 API
- Fire-and-forget 模式，不阻塞 SSE 响应流
"""
import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Optional

import httpx

from app.database import _ensure_db_path

logger = logging.getLogger(__name__)


def _get_db_path() -> str:
    _ensure_db_path()
    from app.database import _db_path
    return _db_path


def _get_kb_config():
    """Lazy-load 知识库集成配置（避免循环导入）"""
    from app.config import _get
    return {
        "api_base": _get("KB_API_BASE", "http://localhost:8080"),
        "api_token": _get("KB_API_TOKEN", ""),
        "auto_ingest": _get("KB_AUTO_INGEST", "true").lower() == "true",
    }


class StreamRecorder:
    """包装 SSE 生成器，同时缓冲事件数据，完成后持久化到 DB + 知识库"""

    def __init__(self, inner: AsyncGenerator, db, user_id: int,
                 session_id: int, user_message: str, model: str,
                 session_title: str = "", session_model: str = ""):
        self.inner = inner
        self.user_id = user_id
        self.session_id = session_id
        self.user_message = user_message
        self.model = model
        # ── 知识库集成：记录会话标题和模型 ──
        self.session_title = session_title
        self.session_model = session_model
        self.start_time = time.time()

        # 缓冲区
        self.thinking_text = ""
        self.tool_events: list = []
        self.content = ""
        self.usage: dict = {}
        self.response_id: Optional[str] = None
        self.status = "completed"

        # 独立的数据库连接（生命周期与流式传输同步）
        self._own_db = None

    async def _get_db(self):
        """获取独立数据库连接"""
        if self._own_db is None:
            import aiosqlite
            db_path = _get_db_path()
            self._own_db = await aiosqlite.connect(db_path)
            self._own_db.row_factory = aiosqlite.Row
            await self._own_db.execute("PRAGMA foreign_keys=ON")
        return self._own_db

    async def _close_db(self):
        """关闭独立数据库连接"""
        if self._own_db is not None:
            await self._own_db.close()
            self._own_db = None

    async def record(self) -> AsyncGenerator[str, None]:
        """包装生成器：yield 相同的 SSE 行，同时缓冲数据"""
        try:
            # 先保存用户消息
            await self._save_user_message()

            async for line in self.inner:
                yield line
                # 解析并缓冲
                await self._parse_line(line)

            # 流结束，保存 assistant 消息
            await self._save_assistant_message()
        finally:
            await self._close_db()

    async def _parse_line(self, line: str):
        """解析 SSE 行，缓冲数据"""
        line = line.strip()
        if not line or line.startswith(":"):
            return
        if not line.startswith("data: "):
            return

        raw = line[6:].strip()
        if raw == "[DONE]":
            return

        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            return

        evt_type = evt.get("type", "")

        if evt_type == "thinking":
            self.thinking_text += evt.get("content", "")
        elif evt_type in ("search", "fetch", "python", "command", "tool_start", "tool_call", "tool_done", "search_done"):
            self.tool_events.append(evt)
        elif evt_type == "content":
            self.content += evt.get("content", "")
        elif evt_type == "done":
            self.usage = evt.get("usage", {})
            self.response_id = evt.get("response_id")
        elif evt_type == "error":
            self.status = "error"

    async def _save_user_message(self):
        """保存用户消息到 DB"""
        db = await self._get_db()
        await db.execute(
            """INSERT INTO messages (session_id, role, content, status)
               VALUES (?, 'user', ?, 'completed')""",
            (self.session_id, self.user_message)
        )
        # 更新会话 updated_at
        await db.execute(
            "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
            (self.session_id,)
        )
        await db.commit()

    async def _save_assistant_message(self):
        """流结束后保存完整的 assistant 消息到 DB"""
        duration_ms = int((time.time() - self.start_time) * 1000)
        usage = self.usage or {}
        usage_details = usage.get("completion_tokens_details", {})

        db = await self._get_db()
        await db.execute(
            """INSERT INTO messages
               (session_id, role, content, model, thinking_text, tool_events,
                prompt_tokens, completion_tokens, total_tokens, reasoning_tokens,
                duration_ms, response_id, status)
               VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.session_id,
                self.content,
                self.model,
                self.thinking_text,
                json.dumps(self.tool_events, ensure_ascii=False),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
                usage_details.get("reasoning_tokens", 0) if isinstance(usage_details, dict) else 0,
                duration_ms,
                self.response_id,
                self.status,
            )
        )
        await db.commit()

        # ── 知识库集成：fire-and-forget 异步 POST，不阻塞 SSE 流 ──
        if self.status == "completed" and self.content:
            asyncio.ensure_future(self._post_to_kb())

    # ── 知识库集成：POST 到 KB 入库 API ──
    async def _post_to_kb(self):
        """将 assistant 消息异步发送到个人知识库入库 API。

        设计原则：
        - Fire-and-forget：不阻塞 SSE 响应流
        - 静默失败：KB 不可达时仅记录日志，不抛异常
        - kb_sent 标记由 kb_retry 调度器兜底
        """
        try:
            config = _get_kb_config()
            if not config["auto_ingest"]:
                return
            if not config["api_token"]:
                logger.debug("KB_API_TOKEN 未配置，跳过知识库导入")
                return

            total_tokens = self.usage.get("total_tokens", 0)
            duration_ms = int((time.time() - self.start_time) * 1000)

            payload = {
                "session_id": self.session_id,
                "message_id": 0,
                "session_title": self.session_title,
                "session_model": self.session_model,
                "content": self.content,
                "thinking_text": self.thinking_text,
                "tool_events": self.tool_events,
                "total_tokens": total_tokens,
                "duration_ms": duration_ms,
                "status": self.status,
                "model": self.model,
            }

            url = f"{config['api_base']}/api/ingest/miromind"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {config['api_token']}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code in (200, 202):
                    logger.info(
                        "KB 入库成功: session=%d tokens=%d",
                        self.session_id, total_tokens,
                    )
                else:
                    logger.warning(
                        "KB 入库失败: session=%d HTTP %d: %s",
                        self.session_id, resp.status_code, resp.text[:200],
                    )
        except Exception:
            logger.debug(
                "KB 入库异常 (session=%d)", self.session_id, exc_info=True,
            )
