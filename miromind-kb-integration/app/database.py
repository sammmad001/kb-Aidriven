"""
SQLite 数据库连接、初始化、依赖注入
使用 aiosqlite 异步驱动 + WAL 模式

变更说明（个人知识库集成）：
- messages 表新增 kb_sent 列（标记是否已发送到知识库）
- _MIGRATIONS 新增 ALTER TABLE 语句，兼容已有数据库
"""
import aiosqlite
from pathlib import Path
from typing import AsyncGenerator

from app.config import DATABASE_PATH

# SQL 建表语句
_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    email         TEXT DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS invite_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    created_by  INTEGER DEFAULT NULL REFERENCES users(id),
    used_by     INTEGER DEFAULT NULL,
    used_at     TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL DEFAULT '新对话',
    model      TEXT NOT NULL DEFAULT 'mirothinker-1-7-deepresearch',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role              TEXT NOT NULL,
    content           TEXT NOT NULL DEFAULT '',
    model             TEXT,
    thinking_text     TEXT DEFAULT '',
    tool_events       TEXT DEFAULT '[]',
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    reasoning_tokens  INTEGER DEFAULT 0,
    duration_ms       INTEGER DEFAULT 0,
    response_id       TEXT,
    status            TEXT NOT NULL DEFAULT 'completed',
    -- 知识库集成：标记是否已发送到 KB（0=未发送, 1=已发送）
    kb_sent           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_user ON sessions(user_id, is_deleted, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_invite_code ON invite_codes(code);
-- 知识库集成：加速 kb_retry 扫描未发送消息
CREATE INDEX IF NOT EXISTS idx_msg_kb_sent ON messages(kb_sent, role, status);
"""

# 增量迁移语句（对已有数据库执行）
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''",
    # 知识库集成：为已有数据库添加 kb_sent 列
    "ALTER TABLE messages ADD COLUMN kb_sent INTEGER NOT NULL DEFAULT 0",
]

_db_path: str = ""


def _ensure_db_path():
    """确保数据库目录存在"""
    global _db_path
    _db_path = DATABASE_PATH
    db_dir = Path(_db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)


async def init_db():
    """初始化数据库：创建表 + 执行增量迁移"""
    _ensure_db_path()
    async with aiosqlite.connect(_db_path) as db:
        await db.executescript(_CREATE_TABLES)
        # 启用 WAL 模式
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA foreign_keys=ON")
        # 增量迁移：对已有数据库添加新列
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # 列已存在则跳过
        await db.commit()


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """FastAPI 依赖注入：获取数据库连接"""
    if not _db_path:
        _ensure_db_path()
    db = await aiosqlite.connect(_db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()
