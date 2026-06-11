# MiroMind → 个人知识库 集成方案文档

---

## 一、背景说明

### 1.1 当前状况

MiroMind 是一个基于 MiroThinker 深度推理模型的研究助手代理，支持多步推理、工具调用（搜索、代码执行等）和流式 SSE 输出。每次对话生成的研究成果（含推理过程、工具调用链、最终结论）目前仅存储在 MiroMind 本地 SQLite 数据库中，形成了**数据孤岛**。

个人知识库（KB）是一个 Graph-First 架构的知识管理系统（Neo4j + FastAPI），已实现完整的 4 步知识入库管道（预处理→分析→图谱处理→渲染），但缺少自动化的多渠道知识汇聚能力。

### 1.2 核心问题

1. **知识断层**：MiroMind 深度研究的成果无法自动回流到个人知识库
2. **手动导入**：用户需要手动复制粘贴或导出再导入，效率极低
3. **数据孤岛**：两个系统各自独立运作，知识无法关联和检索
4. **重复研究**：相似问题被反复提问，历史研究成果未被有效复用

### 1.3 解决方案概述

建立 MiroMind → 个人知识库的**自动化闭环**：每次 MiroMind 对话完成后，自动将助手回复（含推理过程、工具调用链）通过标准化 API 导入到个人知识库，经过语义分析、实体提取、图谱构建后融入知识网络。

---

## 二、需求分析

### 2.1 功能需求

| 需求 | 描述 | 优先级 |
|------|------|--------|
| **自动导入** | 每次对话完成后自动 POST 到 KB | P0 |
| **内容完整性** | 传输完整内容（推理过程 + 工具调用 + 最终回复） | P0 |
| **非阻塞传输** | Fire-and-forget 模式，不阻塞 SSE 响应流 | P0 |
| **失败重试** | KB 不可达时记录失败，定期重试 | P1 |
| **可配置开关** | 通过环境变量控制是否启用集成 | P1 |
| **安全认证** | Bearer Token 认证，防止未授权访问 | P0 |

### 2.2 非功能需求

- **低侵入性**：MiroMind 侧代码变更最小化
- **高容错性**：集成失败不影响 MiroMind 主聊天功能
- **可观测性**：关键操作有日志记录
- **向后兼容**：不启用集成时行为与原来完全一致

### 2.3 覆盖范围

- **触发时机**：每次 assistant 消息写入 DB 后立即触发
- **传输内容**：session_id、session_title、session_model、content、thinking_text、tool_events、total_tokens、duration_ms、status、model
- **KB 侧处理**：接收 → 适配器转换 → SHA256 去重 → 4 步管道 → 入库

---

## 三、实现方案

### 3.1 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         MiroMind (Port 8900)                      │
│                                                                    │
│  ┌──────────────┐    ┌──────────────────┐    ┌─────────────────┐ │
│  │ routers/chat │───▶│ stream_recorder  │───▶│  _post_to_kb()  │ │
│  │ (查询title)  │    │ (缓冲+保存DB)     │    │ (fire-and-forget)│ │
│  └──────────────┘    └──────────────────┘    └───────┬─────────┘ │
│                                                       │           │
│  ┌──────────────────┐                                │           │
│  │   kb_retry.py     │ ← 定时扫描 kb_sent=0 ────────┘           │
│  │ (每5分钟重试)     │         POST /api/ingest/miromind         │
│  └──────────────────┘                                           │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                    个人知识库 KB (Port 8080)                      │
│                                                                    │
│  ┌────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│  │ ingest_adapters│─▶│  KnowledgeAdapter│─▶│  4-Step Pipeline │ │
│  │ /api/ingest/   │  │  extract/validate│  │  analyze→graph   │ │
│  │ miromind       │  │  /transform      │  │  →render         │ │
│  └────────────────┘  └──────────────────┘  └──────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 数据流详解

#### 路径A：事件驱动（主路径）

```
用户发消息 → chat.py 查询 session title/model
           → StreamRecorder 缓冲 SSE 事件
           → _save_assistant_message() 写入 SQLite
           → asyncio.ensure_future(_post_to_kb())  // fire-and-forget
               → POST /api/ingest/miromind
               → KB 侧 MiroMindAdapter 处理
               → 4 步管道 → Neo4j
```

#### 路径B：定时轮询（兜底路径）

```
KbRetrySender 每 5 分钟:
  → SELECT * FROM messages WHERE kb_sent=0 AND status='completed'
  → 逐条 POST /api/ingest/miromind
  → 成功后 UPDATE kb_sent=1
```

### 3.3 代码修改详情

#### 3.3.1 `app/config.py` — 新增配置项

```python
KB_API_BASE: str = _get("KB_API_BASE", "http://localhost:8080")
KB_API_TOKEN: str = _get("KB_API_TOKEN", "")
KB_AUTO_INGEST: str = _get("KB_AUTO_INGEST", "true")
```

#### 3.3.2 `app/database.py` — 数据库扩展

messages 表新增 `kb_sent` 列（`INTEGER NOT NULL DEFAULT 0`），用于追踪消息是否已发送到知识库。新增复合索引 `idx_msg_kb_sent` 加速定时扫描。

兼容性：通过 `_MIGRATIONS` 列表自动执行 `ALTER TABLE`，已有数据库无缝升级。

#### 3.3.3 `app/services/stream_recorder.py` — 核心变更

- `__init__` 新增 `session_title`、`session_model` 参数
- 新增 `_post_to_kb()` 方法：构造 payload → POST 到 KB
- 在 `_save_assistant_message()` 的 `await db.commit()` 之后调用 `asyncio.ensure_future(self._post_to_kb())`
- 所有网络异常静默处理，仅记录 debug 日志

#### 3.3.4 `app/routers/chat.py` — 传递会话信息

在创建 StreamRecorder 之前新增查询：
```python
cursor = await db.execute(
    "SELECT title, model FROM sessions WHERE id = ?", (session_id,)
)
row = await cursor.fetchone()
if row:
    session_title = row["title"]
    session_model = row["model"]
```
将 `session_title`、`session_model` 传给 StreamRecorder。

#### 3.3.5 `app/services/kb_retry.py` — 新增重试调度器（新文件）

KbRetrySender 类：
- 每 300 秒（可配置）扫描 `kb_sent=0` 的 assistant 消息
- 单次最多处理 5 条，防止积压冲击 KB API
- 成功发送后更新 `kb_sent=1`
- 通过 `start()` / `stop()` 控制生命周期

#### 3.3.6 `app/main.py` — 注册调度器

在 lifespan 中：
```python
db = await get_db().__anext__()
_kb_retry = KbRetrySender(db, interval_seconds=300)
_kb_retry.start()
# ...
_kb_retry.stop()
```

### 3.4 安全设计

- **Bearer Token 认证**：与 KB 侧的 `verify_api_token` 依赖注入对齐
- **环境变量隔离**：Token 不在代码中硬编码
- **只传不读**：MiroMind 仅 POST 数据到 KB，不读取 KB 内容

### 3.5 容错设计

| 异常场景 | 处理策略 |
|----------|----------|
| KB 服务未启动 | StreamRecorder 静默失败 + KbRetrySender 5分钟后重试 |
| 网络超时 | httpx 30s 超时，不阻塞 SSE 流 |
| KB 返回非 200 | 记录 warning 日志，不更新 kb_sent |
| 重复发送 | KB 侧 SHA256 content_hash 去重 |
| 数据库迁移失败 | try/except 捕获，跳过（列已存在） |

---

## 四、集成说明

### 4.1 部署流程

详见 [部署步骤.md](./部署步骤.md)

简要步骤：
1. 复制文件到 MiroMind 项目
2. 配置 `.env` 中的 KB_API_BASE / KB_API_TOKEN / KB_AUTO_INGEST
3. 重启服务（数据库自动迁移）
4. 发送测试消息验证

### 4.2 文件交付清单

```
miromind-kb-integration/
├── .env.example                  ← 覆盖 MiroMind 根目录
├── 部署步骤.md                    ← 部署指南
└── app/
    ├── config.py                 ← 覆盖 app/config.py
    ├── database.py               ← 覆盖 app/database.py
    ├── main.py                   ← 覆盖 app/main.py
    ├── routers/
    │   └── chat.py               ← 覆盖 app/routers/chat.py
    └── services/
        ├── stream_recorder.py    ← 覆盖 app/services/stream_recorder.py
        └── kb_retry.py           ← 新文件：app/services/kb_retry.py
```

### 4.3 配置说明

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `KB_API_BASE` | 否 | `http://localhost:8080` | KB 服务地址 |
| `KB_API_TOKEN` | 是 | 空 | 需与 KB 侧 `KNOWLEDGE_API_TOKEN` 一致 |
| `KB_AUTO_INGEST` | 否 | `true` | 设为 `false` 可完全禁用集成 |

### 4.4 验证方法

**1. 日志验证**（MiroMind 侧）：
```
# 成功时
KB 入库成功: session=42 tokens=15000

# KB 不可达时
KB 入库异常 (session=42) ...
```

**2. API 验证**（KB 侧）：
```bash
# 查看全局统计
curl -H "Authorization: Bearer <TOKEN>" http://localhost:8080/api/ingest/status

# 返回示例
{"total_ingested": 15, "total_pending": 0, "total_failed": 2, "by_source": {"miromind": 15}}
```

**3. 数据库验证**（MiroMind 侧）：
```sql
-- 查看哪些消息尚未发送
SELECT id, session_id, kb_sent FROM messages WHERE role='assistant';
```

### 4.5 常见问题

**Q: 如何临时禁用集成？**
A: 在 `.env` 中设置 `KB_AUTO_INGEST=false`，重启即可。

**Q: Token 从哪里获取？**
A: KB 项目 `/kb/.env` 中的 `KNOWLEDGE_API_TOKEN` 值，或通过 KB 管理接口生成。

**Q: 如果 KB 和 MiroMind 不在同一台机器？**
A: 修改 `KB_API_BASE` 为 KB 的实际地址，如 `http://192.168.1.100:8080`。

**Q: 历史消息会补发吗？**
A: 会。KbRetrySender 启动后会扫描所有 `kb_sent=0` 的消息并逐条重发。

---

## 附录：API 契约

### KB 侧接口：`POST /api/ingest/miromind`

**Headers**
```
Authorization: Bearer <KB_API_TOKEN>
Content-Type: application/json
```

**Request Body**
```json
{
  "session_id": 42,
  "message_id": 0,
  "session_title": "量子计算最新进展",
  "session_model": "mirothinker-1-7-deepresearch",
  "content": "量子计算在2025年...",
  "thinking_text": "让我分析量子计算的最新进展...",
  "tool_events": [
    {"type": "search_done", "data": {...}},
    {"type": "tool_done", "name": "python", "result": "..."}
  ],
  "total_tokens": 15000,
  "duration_ms": 45000,
  "status": "completed",
  "model": "mirothinker-1-7-deepresearch"
}
```

**Response**
- `200` — 入库成功
- `202` — 已接收，处理中
- `400` — 参数校验失败
- `401` — Token 无效
- `409` — 内容重复（content_hash 已存在）
