# DeepSeek LLM 迁移方案文档

> **版本**：v1.0 | **日期**：2026-06-16 | **状态**：已实施

---

## 1. 迁移背景

知识库系统 V1.0/V1.1 时代使用 **Ollama (Qwen2.5:7B)** 作为本地推理引擎，部分场景使用 **DashScope (百炼)** 作为云端补充。随着系统演进，本地 Ollama 暴露出以下问题：

- **推理质量不足**：7B 模型在复杂知识抽取和隐式关系推理场景中幻觉率较高
- **GPU 资源占用**：ECS 无 GPU，Ollama 降级为 CPU 推理延迟达 30-60s/次
- **部署复杂度**：需要在服务器安装 Ollama + 拉取模型，增加运维负担
- **不可水平扩展**：本地模型无法随 API 流量自动扩缩容

迁移到 **DeepSeek API**（OpenAI 兼容接口）后，所有 LLM 调用通过云端 API 完成，延迟稳定在 2-8s，推理质量显著提升。

### DashScope 保留范围

DashScope API Key 仍然保留，但仅用于 **非 LLM 服务**：

| 用途 | 模型 | 说明 |
|------|------|------|
| ASR 语音转写 | `paraformer-realtime-v2` | 飞书语音消息转文字 |
| OCR 图片识别 | `qwen-vl-max` | 社交媒体图片文字提取（PaddleOCR 兜底） |

---

## 2. 架构变更

### 2.1 DeepSeekClient

新增 [DeepSeekClient](kb/app/llm.py#L244-L291) 类，实现 OpenAI Chat Completions 兼容接口：

```python
class DeepSeekClient(LLMClient):
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com"):
        self._client = httpx.AsyncClient(timeout=30.0)
```

- **API 端点**：`{base_url}/v1/chat/completions`
- **认证方式**：`Authorization: Bearer {api_key}`
- **超时**：30s（DeepSeek API 典型响应 2-8s）
- **非流式**：`stream: false`，单次请求等待完整 JSON 响应
- **JSON 模式**：支持 `response_format: {"type": "json_object"}`
- **Token 追踪**：V1.2 新增，从 API 响应 `usage` 字段提取 prompt/completion/total tokens

### 2.2 ResilientLLMClient 封装

[ResilientLLMClient](kb/app/llm.py#L160-L241) 为所有 LLM 调用提供自动容错：

**DeepSeek Fallback 链**：
```
deepseek-v4-pro (1次重试, 1s退避)
  → deepseek-v4-flash (1次重试, 2s退避)
```

**工作机制**：
1. 首先使用请求指定的模型尝试调用
2. 若返回 `TimeoutException` 或 `HTTPStatusError`（5xx），按 fallback 链依次降级
3. 每层指数退避：`backoff = base_backoff * (2 ** attempt)`
4. Token 用量在多次 fallback 中累加，最终写回 `_usage` 对象
5. 所有层级耗尽后抛出 `RuntimeError("All model tiers exhausted")`

### 2.3 工厂函数

[get_llm_client()](kb/app/llm.py#L294-L311) 根据 `llm_provider` 配置创建对应客户端：

| `llm_provider` | 客户端类 | ResilientLLMClient |
|----------------|----------|---------------------|
| `deepseek` | DeepSeekClient | ✅ DeepSeek fallback 链 |
| `dashscope` | DashScopeClient | ✅ DashScope fallback 链（向后兼容） |
| `ollama` | OllamaClient | ❌ 无 fallback |

### 2.4 独立意图识别 LLM

[get_intent_llm_client()](kb/app/llm.py#L314-L338) 为意图识别模块创建专用客户端，独立于主管线：

```python
intent_llm_provider: Literal["dashscope", "deepseek"] = "deepseek"
intent_llm_model: str = "deepseek-v4-flash"
```

- 支持使用不同于主管线的模型（通常更轻量/便宜）
- 若 DeepSeek Key 为空，自动降级到 DashScope
- 若两个 Key 均为空，返回 None，意图检测器仅使用规则模式

---

## 3. 分环节模型分配

DeepSeek 支持按流水线环节分配不同模型，平衡成本和质量：

| 环节 | 配置项 | 默认模型 | 说明 |
|------|--------|----------|------|
| Step 2 分析分类 | `deepseek_model_analyze` | `deepseek-v4-flash` | 轻量任务，快速响应 |
| Step 3b 页面编写 | `deepseek_model_compile` | `deepseek-v4-pro` | 质量关键，需强推理 |
| Step 3c 隐式推理 | `deepseek_model_reasoning` | `deepseek-v4-pro` | 核心推理，需强推理 |
| Query 回答生成 | `deepseek_model_query` | `deepseek-v4-pro` | 用户面向，需高质量 |
| 意图识别 | `intent_llm_model` | `deepseek-v4-flash` | 快速判断，2s超时 |

---

## 4. 配置变更

### 新增环境变量

```bash
# DeepSeek API（主 LLM 提供商）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-api-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro

# 分环节模型
DEEPSEEK_MODEL_ANALYZE=deepseek-v4-flash
DEEPSEEK_MODEL_COMPILE=deepseek-v4-pro
DEEPSEEK_MODEL_REASONING=deepseek-v4-pro
DEEPSEEK_MODEL_QUERY=deepseek-v4-pro

# 意图识别 LLM（独立配置）
INTENT_LLM_PROVIDER=deepseek
INTENT_LLM_MODEL=deepseek-v4-flash
```

### 保留环境变量（DashScope 仅 ASR/OCR）

```bash
DASHSCOPE_API_KEY=your-dashscope-key     # 仅用于 ASR/OCR
ASR_ENABLED=true
ASR_MODEL=paraformer-realtime-v2
SOCIAL_OCR_DASHSCOPE_KEY=your-key        # OCR 兜底用
```

### 已废弃环境变量（向后兼容保留）

```bash
# 以下配置项已迁移到 DeepSeek，保留仅为向后兼容
DASHSCOPE_MODEL_ANALYZE=qwen-turbo       # 已废弃
DASHSCOPE_MODEL_COMPILE=qwen3.5-plus      # 已废弃
DASHSCOPE_MODEL_REASONING=qwen3.5-plus    # 已废弃
DASHSCOPE_MODEL_QUERY=qwen3.5-plus        # 已废弃
```

---

## 5. 影响文件清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `kb/app/config.py` | 新增配置 | DeepSeek 系列 + 意图识别 LLM 配置项 |
| `kb/app/llm.py` | 新增类 | DeepSeekClient + ResilientLLMClient DeepSeek 分支 + get_intent_llm_client |
| `kb/app/ingest/analyze.py` | 修改 | 模型引用从 dashscope_model_* 改为 deepseek_model_* |
| `kb/app/ingest/graph_process.py` | 修改 | LLM 富化和隐式推理模型引用改为 DeepSeek |
| `kb/app/query/understand.py` | 修改 | 查询理解 LLM 模型改为 deepseek_model_analyze |
| `kb/app/query/generate.py` | 修改 | 答案生成 LLM 模型改为 deepseek_model_query |
| `kb/app/feishu/intent.py` | 修改 | 意图识别 LLM 使用独立配置 |

---

## 6. 迁移验证

1. **配置验证**：`settings.validate_production_config()` 确保 `DEEPSEEK_API_KEY` 已设置
2. **连通性测试**：发送一条 `/q 什么是RAG` 查询，确认收到 DeepSeek 生成的回答
3. **Fallback 测试**：手动断网或使用错误 API Key，验证 fallback 链日志输出
4. **ASR/OCR 验证**：发送语音消息或社交媒体链接，确认 DashScope ASR/OCR 仍正常工作
5. **意图识别验证**：发送模糊文本（如"帮我研究量子计算"），确认正确路由到 research
