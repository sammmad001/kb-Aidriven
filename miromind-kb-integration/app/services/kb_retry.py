"""
轻量级 KB 重发调度器

定期扫描 messages 表中 kb_sent=0 的 assistant 消息，重试 POST 到知识库。
处理场景：
- KB 服务暂时不可达导致 StreamRecorder._post_to_kb() 静默失败
- 网络波动导致首次 POST 未成功
- 启动时补发历史未发送消息

设计原则：
- 单次最多重试 5 条，避免积压过多时冲击 KB API
- 成功发送后标记 kb_sent=1
- 所有异常静默处理，不影响 MiroMind 主流程
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class KbRetrySender:
    """定期扫描未发送到知识库的消息并重试。

    使用方式（在 main.py lifespan 中）：
        retry = KbRetrySender(db, interval_seconds=300)
        retry.start()
        # ... 应用运行 ...
        retry.stop()
    """

    def __init__(self, db, interval_seconds: int = 300):
        """
        Args:
            db: aiosqlite 数据库连接
            interval_seconds: 扫描间隔（秒），默认 300（5 分钟）
        """
        self._db = db
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    async def _scan_and_retry(self):
        """扫描未发送消息并逐条重试 POST 到 KB。
        单次最多处理 5 条，防止积压过多。
        """
        from app.config import KB_API_BASE, KB_API_TOKEN, KB_AUTO_INGEST

        if KB_AUTO_INGEST.lower() != "true" or not KB_API_TOKEN:
            return

        try:
            cursor = await self._db.execute(
                """SELECT m.id, m.session_id, m.content, m.thinking_text,
                          m.tool_events, m.total_tokens, m.duration_ms,
                          m.status, m.model,
                          s.title AS session_title, s.model AS session_model
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE m.role = 'assistant'
                     AND m.kb_sent = 0
                     AND m.status = 'completed'
                   ORDER BY m.created_at ASC
                   LIMIT 5"""
            )
            rows = await cursor.fetchall()

            for row in rows:
                payload = {
                    "session_id": row["session_id"],
                    "message_id": row["id"],
                    "session_title": row["session_title"] or "新对话",
                    "session_model": row["session_model"] or "",
                    "content": row["content"] or "",
                    "thinking_text": row["thinking_text"] or "",
                    "tool_events": row["tool_events"] or [],
                    "total_tokens": row["total_tokens"] or 0,
                    "duration_ms": row["duration_ms"] or 0,
                    "status": row["status"] or "completed",
                    "model": row["model"] or "",
                }

                url = f"{KB_API_BASE}/api/ingest/miromind"
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.post(
                            url,
                            json=payload,
                            headers={
                                "Authorization": f"Bearer {KB_API_TOKEN}",
                            },
                        )
                    if resp.status_code in (200, 202):
                        await self._db.execute(
                            "UPDATE messages SET kb_sent = 1 WHERE id = ?",
                            (row["id"],),
                        )
                        await self._db.commit()
                        logger.info("KB 重发成功: msg=%d session=%d",
                                    row["id"], row["session_id"])
                except Exception:
                    logger.debug("KB 重发失败: msg=%d", row["id"])
        except Exception:
            logger.debug("KB 重试扫描异常", exc_info=True)

    async def _loop(self):
        """循环：等待 interval 秒后执行扫描"""
        while True:
            await asyncio.sleep(self._interval)
            await self._scan_and_retry()

    def start(self):
        """启动重试调度器（后台 asyncio task）"""
        self._task = asyncio.ensure_future(self._loop())
        logger.info("KB 重试调度器已启动（间隔=%ds）", self._interval)

    def stop(self):
        """停止重试调度器"""
        if self._task:
            self._task.cancel()
            logger.info("KB 重试调度器已停止")
