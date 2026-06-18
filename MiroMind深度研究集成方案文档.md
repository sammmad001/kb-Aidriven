# MiroMind 深度研究集成方案文档

> **版本**：v1.0
> **日期**：2025-01-25
> **状态**：已实现
> **关联模块**：`kb/app/services/miromind_client.py`、`kb/app/adapters/miromind.py`、`kb/app/api/ingest_adapters.py`、`kb/app/feishu/research_cards.py`

---

## 1. 设计背景

用户在飞书 Bot 中提出深度研究需求时（如"深度研究 RAG 架构"），系统需要：

1. **调用 MiroMind API** 进行深度研究（联网搜索 + 多轮推理）
2. **将研究结果结构化** 提取为知识库可摄入的格式
3. **自动入库** 经验证通过的研究结果通过 Ingest 管道写入 Neo4j
4. **卡片展示** 在飞书中以富文本卡片形式展示研究结果和入库状态

MiroMind 是一个 OpenAI Chat Completions 兼容的深度研究 API，支持联网搜索、Python 执行、多轮推理等能力。

---

## 2. 整体架构

```
用户飞书消息
    │
    │ "深度研究 RAG 架构"
    │ 或 "/research RAG 架构"
    ▼
┌─────────────────────────────────────────────┐
│ feishu/handlers.py                          │
│  └─ IntentDetector.detect() → "research"    │
│  └─ _handle_research()                      │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ services/miromind_client.py                 │
│  └─ MiroMindClient.research(question)       │
│      └─ POST /v1/chat/completions           │
│      └─ 注入 LENGTH_CONSTRAINT (≤2000字)     │
│      └─ 解析 → ResearchResult               │
└─────────────────┬───────────────────────────┘
                  │
          ┌───────┴───────┐
          │               │
          ▼               ▼
┌──────────────┐  ┌───────────────────────────┐
│ research_    │  │ adapters/miromind.py      │
│ cards.py     │  │  └─ MiroMindAdapter       │
│ └─ 飞书卡片   │  │      extract() → Markdown │
│    展示      │  │      validate() → 质量门   │
└──────────────┘  │      transform() → Source │
                  └───────────┬───────────────┘
                              │
                              ▼
                  ┌───────────────────────────┐
                  │ api/ingest_adapters.py    │
                  │  └─ POST /api/ingest/     │
                  │       miromind            │
                  │  └─ BackgroundTasks       │
                  │  └─ IngestTracker         │
                  └───────────┬───────────────┘
                              │
                              ▼
                  ┌───────────────────────────┐
                  │ Knowledge Pipeline        │
                  │  └─ Preprocessor          │
                  │  └─ EntityExtractor       │
                  │  └─ GraphProcessor → Neo4j│
                  └───────────────────────────┘
```

---

## 3. MiroMindClient — API 客户端

### 3.1 类设计

```python
class MiroMindClient:
    """非流式 MiroMind API 客户端（OpenAI Chat Completions 格式）"""

    def __init__(
        self,
        api_base: str | None = None,      # 默认: settings.miromind_api_base
        api_key: str | None = None,       # 默认: settings.miromind_api_key
        default_model: str | None = None,  # 默认: settings.miromind_default_model
        timeout: float | None = None,     # 默认: settings.miromind_request_timeout
    ) -> None
```

### 3.2 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `miromind_api_base` | — | MiroMind API 基础 URL |
| `miromind_api_key` | — | API 密钥 |
| `miromind_default_model` | `mirothinker-1-7-deepresearch-mini` | 默认研究模型 |
| `miromind_request_timeout` | `300` | 请求超时（秒），深度研究耗时较长 |

### 3.3 research() 方法

```python
async def research(self, question: str, model: str | None = None) -> ResearchResult:
```

**流程**：

1. **配置检查**：`is_configured` 验证 API Key 是否存在
2. **注入长度约束**：`LENGTH_CONSTRAINT + "\n\n" + question` 作为 user message
3. **发送请求**：`httpx.AsyncClient` 单次 POST 到 `/chat/completions`
4. **解析响应**：提取 `choices[0].message.content`，构建 `ResearchResult`
5. **错误处理**：HTTP 非 200 / TimeoutException / 其他异常 → 返回 `status="error"` 的 ResearchResult

### 3.4 长度约束（LENGTH_CONSTRAINT）

```python
LENGTH_CONSTRAINT = (
    "[Output Requirement] Please keep your response concise and well-structured. "
    "The total output must not exceed 2000 characters. Focus on key findings "
    "and actionable insights. Use clear headings and bullet points."
)
```

**设计目的**：
- MiroMind 深度研究可能产出数万字的长文
- 知识库摄入需要精炼内容（避免 token 浪费、提高检索质量）
- 飞书卡片展示也有字符限制

### 3.5 ResearchResult 数据结构

```python
@dataclass
class ResearchResult:
    content: str           # 主要研究正文（≤2000字）
    thinking_text: str     # 推理摘要（当前为空，预留）
    total_tokens: int      # Token 用量
    status: str            # completed / error / failed
    model: str             # 使用的模型
    duration_ms: int       # 请求耗时（毫秒）
    error: str | None      # 错误信息
    tool_events: list[dict] # 工具调用事件（搜索/抓取/Python等）
```

`to_miromind_payload()` 方法将 ResearchResult 转为 `MiroMindMessagePayload` 兼容格式，供适配器使用。

### 3.6 响应解析

```python
def _parse_response(self, data, model, duration_ms) -> ResearchResult:
```

从 OpenAI Chat Completions 标准响应中提取：

```
{
    "choices": [{
        "message": {
            "content": "研究正文..."
        }
    }],
    "usage": {"total_tokens": 1234},
    "model": "mirothinker-1-7-deepresearch-mini"
}
```

---

## 4. MiroMindAdapter — 知识适配器

### 4.1 职责

将 MiroMind 研究结果转换为知识库可摄入的结构化格式，并执行质量验证。

### 4.2 类定义

```python
class MiroMindAdapter(KnowledgeAdapter):
    def __init__(self, min_tokens: int = 500) -> None

    @property
    def source_type(self) -> str  # 返回 "miromind"

    async def extract(self, source_data: dict) -> ExtractedKnowledge
    async def validate(self, extracted: ExtractedKnowledge) -> tuple[bool, str]
    async def transform(self, extracted: ExtractedKnowledge) -> tuple[Source, dict]
```

### 4.3 extract() — 结构化提取

将原始 payload 转换为结构化 Markdown 文档：

```
# {session_title}

{content（研究正文）}

---

## 思考过程（如有 thinking_text）

{thinking_text}

---

## 研究过程（如有 tool_events）

- 🔍 搜索: keyword1, keyword2
- 📄 获取: https://example.com/...
- 🐍 Python 执行
- 🔧 工具调用: tool_name

---

> 🤖 {model} | Token: {total_tokens} | 耗时: {duration_ms}s | [原始对话](miromind://session/{session_id}#msg-{message_id})
```

**工具事件解析**：

| 事件类型 | 图标 | 显示内容 |
|---------|------|---------|
| `search` | 🔍 | 关键词列表 |
| `fetch` | 📄 | 抓取的 URL |
| `python` | 🐍 | Python 执行 |
| `tool_call` | 🔧 | 工具名称 |
| 其他 | 📌 | 事件类型 |

### 4.4 validate() — 质量验证

| 检查项 | 条件 | 失败原因 |
|--------|------|---------|
| 状态检查 | `status` 非 error/interrupted | `"message status is '{status}'"` |
| 内容非空 | `content` 存在且非空 | `"empty content"` |
| Token 阈值 | `total_tokens ≥ 500`（DEFAULT_MIN_TOKENS） | `"token count too low"` |
| 非纯思考 | 内容不仅是 thinking_text | `"thinking only, no final answer content"` |

**设计考量**：
- `min_tokens=500`：过滤掉过短的研究结果（可能是失败的研究或简单回复）
- 纯 thinking 过滤：防止将仅包含推理过程但无最终结论的结果入库

---

## 5. Ingest 端点 — API 入口

### 5.1 POST /api/ingest/miromind

```python
@router.post("/miromind", status_code=202)
async def ingest_miromind(
    payload: MiroMindMessagePayload,
    bg: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user_or_service),
) -> dict:
```

**返回 HTTP 202 Accepted**，实际处理在后台异步执行。

### 5.2 MiroMindMessagePayload

```python
class MiroMindMessagePayload(BaseModel):
    session_id: int
    message_id: int
    session_title: str = "MiroMind 研究"
    session_model: str = ""
    content: str = ""
    thinking_text: str = ""
    tool_events: list[dict] = []
    total_tokens: int = 0
    duration_ms: int = 0
    status: str = "completed"
    model: str = ""
```

### 5.3 处理流程

```
1. extract()    → 提取结构化知识
2. validate()   → 质量验证
   ├─ 不通过 → mark_skipped() → return {"status": "skipped"}
3. 持久化原始 JSON → raw/ingest/miromind/{source_id}.json
4. record_attempt() → 幂等检查（已完成/已跳过 → return）
5. transform() → 转为 IngestRequest
6. BackgroundTasks 异步执行 pipeline.run()
   ├─ 成功 → mark_completed()
   └─ 失败 → mark_failed()
```

### 5.4 幂等保证

通过 IngestTracker 的 `record_attempt()` 实现：
- `source_id = "{session_id}:{message_id}"`
- 已存在且状态为 `completed` / `skipped` → 直接返回，不重复处理
- 已存在且状态为 `processing` → 返回"处理中"
- 不存在 → 创建新记录，继续处理

### 5.5 监控端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/ingest/status` | GET | 全量入库统计 |
| `/api/ingest/status/{source_id}` | GET | 单条入库记录状态 |
| `/api/ingest/retry/{source_id}` | POST | 手动重试单条失败记录 |

---

## 6. 飞书研究卡片

### 6.1 卡片类型

#### build_research_result_card() — 成功卡片

```
┌─────────────────────────────────────────┐
│ 🔬 MiroMind 深度研究               (靛蓝) │
├─────────────────────────────────────────┤
│ 🔍 研究问题                              │
│ {question}                               │
│ ───────────────────────────              │
│ {content（≤3000字符）}                    │
│ ───────────────────────────              │
│ Token: **1234** | 耗时: **45.2s** |      │
│ 模型: `mirothinker-1-7-deepresearch-mini`│
│ ───────────────────────────              │
│ 📚 知识入库: 已入库 3 个知识点             │
└─────────────────────────────────────────┘
```

#### build_research_unavailable_card() — 不可用卡片

```
┌─────────────────────────────────────────┐
│ 🔬 MiroMind 不可用                 (橙色) │
├─────────────────────────────────────────┤
│ MiroMind 深度研究功能未启用。             │
│                                          │
│ 可能原因：                                │
│ - MIROMIND_API_KEY 未配置                │
│ - MiroMind API 服务不可达                │
│                                          │
│ 请联系管理员配置后重试。                   │
└─────────────────────────────────────────┘
```

### 6.2 错误处理

当 `result.status == "error"` 时，卡片以红色标题显示：
```
┌─────────────────────────────────────────┐
│ 🔬 深度研究                        (红色) │
├─────────────────────────────────────────┤
│ ❌ 研究失败: {result.error}               │
└─────────────────────────────────────────┘
```

---

## 7. 触发方式

### 7.1 自然语言触发

意图识别中 RESEARCH 模式匹配后自动路由：

| 用户输入 | 匹配规则 | 路由 |
|---------|---------|------|
| "深度研究 RAG 架构" | `深度研究` | research |
| "帮我研究一下向量数据库" | `帮我研究` | research |
| "用 MiroMind 分析 Transformer" | `用MiroMind` | research |
| "全面分析 LLM 选型？" | `全面分析`（RESEARCH 优先于 QUERY） | research |

### 7.2 显式命令触发

```
/research RAG 架构的发展趋势
```

---

## 8. 与知识库管道的集成

### 8.1 数据流向

```
MiroMind 研究
    │
    ▼
MiroMindClient.research() → ResearchResult
    │
    ▼
ResearchResult.to_miromind_payload() → MiroMindMessagePayload
    │
    ▼
MiroMindAdapter.extract() → ExtractedKnowledge（Markdown）
    │
    ▼
MiroMindAdapter.validate() → 通过 / 跳过
    │
    ▼
MiroMindAdapter.transform() → (Source, options)
    │
    ▼
Pipeline.run(IngestRequest)
    ├─ Preprocessor → 文本分段
    ├─ EntityExtractor → 实体抽取
    └─ GraphProcessor → Neo4j 写入（user_id 隔离）
```

### 8.2 多用户隔离

研究结果的入库遵循多用户数据隔离机制：
- `current_user` 通过 `get_current_user_or_service` 获取
- Neo4j 写入时自动注入 `user_id`
- 原始 JSON 持久化到 `raw/ingest/miromind/{user_id}/` 目录

---

## 9. 配置项清单

| 环境变量 | 配置项 | 说明 |
|---------|--------|------|
| `MIROMIND_API_BASE` | `miromind_api_base` | API 基础 URL |
| `MIROMIND_API_KEY` | `miromind_api_key` | API 密钥 |
| `MIROMIND_DEFAULT_MODEL` | `miromind_default_model` | 默认模型 |
| `MIROMIND_REQUEST_TIMEOUT` | `miromind_request_timeout` | 请求超时（默认 300s） |

---

## 10. 影响文件清单

| 文件 | 行数 | 角色 |
|------|------|------|
| `kb/app/services/miromind_client.py` | 247 | MiroMind API 客户端（非流式，OpenAI 兼容） |
| `kb/app/adapters/miromind.py` | 166 | 知识适配器（extract/validate/transform） |
| `kb/app/api/ingest_adapters.py` | 268 | Ingest API 端点 + IngestTracker 集成 |
| `kb/app/feishu/research_cards.py` | 77 | 飞书研究卡片构建器 |
| `kb/app/feishu/handlers.py` | — | `_handle_research()` 路由处理 |
| `kb/app/feishu/intent.py` | 159 | RESEARCH 意图检测 |
| `kb/app/config.py` | 143 | MiroMind 配置项定义 |
