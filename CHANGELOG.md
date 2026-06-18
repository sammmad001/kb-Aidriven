# Changelog

本文件记录个人知识库系统的所有显著变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh/1.1.0/)。

---

## [Unreleased]

_无待发布内容。_

---

## [1.2.5] - 2026-06-18

### fix
- 配置默认值修正 + .env.example补全 + 文档审计修复 ([v1.2.5])
  - 发布时间: 2026-06-18
  - 优先级: P0
  - 部署状态: pending
  - 修改文件: `config.py`, `.env.example`, `deploy-ecs.sh`, 6篇方案文档
  - 主要变更:
    - config.py: llm_provider 默认值 ollama → deepseek
    - config.py: miromind_default_model 更新为 mirothinker-1-7-deepresearch
    - .env.example: 补全 Environment/Paths/Ingest Automation 配置项 (+12行)
    - deploy-ecs.sh: 添加 deprecated 标记
    - 文档审计修复: 产品存档/V1.1推理/系统架构/输入流/输出流/飞书交互
    - 新增设计文档: DeepSeek迁移/MiroMind集成/V1.2查询管道/多用户认证/意图识别/社交抓取
  - 验证: pytest 176/176 通过, py_compile 59/59 通过

---

## [1.2.4] - 2026-06-18

### feat
- 社交媒体抓取（小红书/微博）+ OCR 双引擎 — SocialFetcher + PaddleOCR/qwen-vl-max ([v1.2.4])
  - 发布时间: 2026-06-18
  - 优先级: P1
  - 部署状态: pending
  - 新增文件: `services/social_fetcher.py`, `ingest/ocr.py`
  - 主要变更:
    - SocialFetcher: Playwright headless Chromium + playwright-stealth 反检测
    - URL 检测: 正则匹配 xhslink.com / xiaohongshu.com / weibo.com
    - 小红书流程: 短链解析 → Cookie 注入 → DOM 提取 → 图片下载
    - 微博流程: JSON API 优先 → Playwright fallback
    - OCR 双引擎: PaddleOCR 免费本地 → qwen-vl-max 付费 fallback（置信度阈值 0.6）
  - 配置项: `SOCIAL_XHS_COOKIE`, `SOCIAL_WEIBO_COOKIE`, `PADDLEOCR_ENABLED`
  - 设计文档: `社交媒体抓取与OCR方案文档.md`

---

## [1.2.3] - 2026-06-17

### feat
- MiroMind 深度研究集成 — MiroMindClient + Adapter + IngestTracker + 研究卡片渲染 ([v1.2.3])
  - 发布时间: 2026-06-17
  - 优先级: P1
  - 部署状态: pending
  - 新增文件: `services/miromind_client.py`, `adapters/miromind.py`, `feishu/research_cards.py`
  - 主要变更:
    - MiroMindClient: OpenAI Chat Completions 标准格式，非流式，300s 超时
    - MiroMindAdapter: extract/validate/transform，ExtractedKnowledge dataclass
    - Ingest 端点: `POST /api/ingest/miromind`，BackgroundTasks 异步处理
    - IngestTracker: 幂等记录、状态跟踪、raw JSON 持久化
    - 飞书命令: `/research <主题>` 触发深度研究 → 研究卡片 → 自动 ingest
  - 配置项: `MIROMIND_API_KEY`, `MIROMIND_MODEL`, `MIROMIND_BASE_URL`
  - 设计文档: `MiroMind深度研究集成方案文档.md`

---

## [1.2.2] - 2026-06-17

### feat
- 多用户认证体系 — JWT + SQLite UserStore + Neo4j 数据隔离 + 飞书 open_id 绑定 ([v1.2.2])
  - 发布时间: 2026-06-17
  - 优先级: P0
  - 部署状态: pending
  - 新增模块: `auth/`（user_store.py, jwt_handler.py, dependencies.py, models.py）
  - 主要变更:
    - JWT 认证: access token (30min) + refresh token (7d)，python-jose + bcrypt
    - 用户存储: SQLite UserStore，飞书 open_id 映射
    - 认证中间件: `get_current_user`, `get_current_user_with_rate_limit`, `get_current_user_or_service`
    - 速率限制: Token bucket，ingest 10/60s，query 30/60s
    - Neo4j 隔离: ContextVar user_id 注入 + 复合唯一约束 `(n.id, n.user_id)`
    - 文件系统隔离: `raw/sources/{user_id}/` 分目录存储
    - 飞书命令: `/register`, `/bind`, `/unbind`, `/whoami`
  - 配置项: `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_ACCESS_EXPIRE`, `JWT_REFRESH_EXPIRE`
  - 设计文档: `多用户认证与数据隔离方案文档.md`

---

## [1.2.1] - 2026-06-16

### feat
- DeepSeek API 迁移 — 从 Ollama/DashScope 迁移到 DeepSeek 统一架构 ([v1.2.1])
  - 发布时间: 2026-06-16
  - 优先级: P0
  - 部署状态: pending
  - 修改文件: `config.py`, `llm.py`, `query/pipeline.py`, `ingest/analyze.py`, `ingest/graph_process.py`
  - 主要变更:
    - DeepSeekClient: OpenAI 兼容 API，30s 超时
    - ResilientLLMClient: 新增 `_DEEPSEEK_FALLBACKS` 回退链（deepseek-v4-pro → deepseek-v4-flash）
    - 模型分配: analyze=deepseek-v4-flash, compile/query=deepseek-v4-pro, reasoning=deepseek-v4-pro
    - 意图识别: deepseek-v4-flash（2s 超时，JSON 输出）
    - DashScope 仅保留: ASR (Paraformer) + OCR (qwen-vl-max)
    - 配置项: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL_*`, `DEEPSEEK_BASE_URL`
  - 设计文档: `DeepSeek迁移方案文档.md`

---

## [1.2.0] - 2026-06-15

### feat
- V1.2 查询管道优化 — EntityResolver + QualityGate + PipelineTrace 五步架构 ([v1.2.0])
  - 发布时间: 2026-06-15
  - 优先级: P0
  - 部署状态: pending
  - 新增文件: `query/understand.py`, `query/resolve.py`, `query/gate.py`, `query/trace.py`
  - 修改文件: `query/pipeline.py`, `query/retrieve.py`, `query/generate.py`
  - 主要变更:
    - Step 1 查询理解: jieba POS 标注 + 规则+LLM 混合分类，ambiguity 阈值 30→15
    - Step 1.5 EntityResolver: 3 层匹配（精确 → CONTAINS → difflib 模糊，阈值 0.6）+ 搜索建议
    - Step 2 图谱检索: 4 策略 dispatch（factual/relational/reasoning/global），多用户 user_id 隔离
    - Step 2.5 QualityGate: GateDecision 枚举（SUFFICIENT/PARTIAL/INSUFFICIENT），factual 快速路径 0 次 LLM
    - Step 3 答案生成: deepseek-chat，对话上下文 follow-up 富化
    - PipelineTrace: trace_id + step-level timing + 结构化日志
  - 设计文档: `V1.2查询管道优化方案文档.md`

### feat
- 三层意图识别 + 对话上下文管理 — IntentDetector + ConversationContext ([v1.1.9])
  - 发布时间: 2026-06-15
  - 优先级: P1
  - 部署状态: pending
  - 新增文件: `feishu/intent.py`, `feishu/context.py`
  - 主要变更:
    - 三层意图识别: 显式前缀 0ms → 规则引擎 0ms → LLM ~300ms（2s 超时）
    - 意图分类: query / input / research / social
    - ConversationContext: per-user OrderedDict, max_turns=5, TTL=600s
    - Follow-up 富化: 指代词替换 + 上下文实体继承
  - 设计文档: `意图识别与对话上下文方案文档.md`

### feat
- 飞书 WebSocket 长连接 — FeishuWSClient 替代公网 Webhook ([v1.1.8b])
  - 发布时间: 2026-06-16
  - 优先级: P1
  - 部署状态: pending
  - 新增文件: `feishu/ws_client.py`
  - 主要变更: auto-reconnect + watchdog 心跳 + 线程安全 dispatch

### feat
- add Web UI login flow with JWT auth: LoginPage + AuthContext + ProtectedRoute + token auto-refresh ([v1.1.8])
  - 发布时间: 2026-06-16
  - 优先级: P1
  - 部署状态: deployed
  - 新增文件: auth/AuthContext.tsx, auth/ProtectedRoute.tsx, pages/LoginPage.tsx
  - 修改文件: api/client.ts, main.tsx, router.tsx, layouts/AppLayout.tsx

### fix
- fix CD pipeline: add setup-node step for frontend build + ensure dist target dir exists ([v1.1.7])
  - 发布时间: 2026-06-16
  - 优先级: P0
  - 部署状态: deployed
  - 根因: GitHub Actions runner 默认 Node.js 20，项目需要 Node.js 22（Vite 8 + TypeScript 6）

### fix
- mount frontend dist on FastAPI + CD pipeline frontend build + XHS redirect loop guard ([v1.1.6])
  - 发布时间: 2026-06-16
  - 优先级: P0
  - 部署状态: deployed

### fix
- fix social URL detection in handle_text for plain-text shares ([v1.1.5])
  - 发布时间: 2026-06-16
  - 优先级: P1
  - 部署状态: deployed

### fix
- 修复对话上下文时间戳碰撞+消息顺序错乱+研究测试mock ([v1.1.4])
  - 发布时间: 2026-06-16
  - 优先级: P0
  - 部署状态: deployed

### fix
- 8维度审计修复: 安全加固/代码质量/功能补全/测试覆盖/lint清理 ([v1.1.1])
  - 发布时间: 2026-06-15
  - 优先级: P0
  - 部署状态: deployed

### feat
- V1.1 知识推理优化 — 回退链+三级匹配+9类隐式关系+Reviewer+端到端验证 ([v1.1.0])
  - 发布时间: 2026-06-12
  - 优先级: P2
  - 部署状态: deployed

---

## [1.0.1] - 2026-06-10

### fix
- 生产校验 CORS 警告与错误分离，不阻止启动 ([6d75058](https://github.com/sammmad001/kb-Aidriven/commit/6d75058))
  - 影响范围: `app/config.py`, `app/main.py`
  - 验证: pytest PASSED, /health ok
  - 部署状态: deployed

---

## [1.0.0] - 2026-06-10

### feat
- V1.0 全面代码质量与安全加固 — 57 项审计发现修复 ([5f9523c](https://github.com/sammmad001/kb-Aidriven/commit/5f9523c))
  - 影响范围: 全模块（31 files, +632 -258）
  - 主要变更:
    - 认证安全修复（空 token 绕过、timing-safe 比较、全端点认证）
    - 输入验证与 SSRF 防护
    - 配置安全加固（生产模式校验）
    - asyncio 资源管理（create_task 引用保存、httpx client 关闭）
    - 错误处理统一（HTTPException 状态码、全局异常捕获）
    - 数据库性能优化（连接池、批量查询、分页限制）
    - 死代码清理
  - 验证: pytest 全通过, 前端构建成功, 无硬编码密钥
  - 部署状态: deployed

### deploy
- V1.0 初始发布 ([510f803](https://github.com/sammmad001/kb-Aidriven/commit/510f803))
  - 影响范围: 全部代码
  - 验证: 健康检查通过, ECS 部署成功
  - 部署状态: deployed

---

## 版本说明

| 标签 | 含义 |
|------|------|
| `CRITICAL` | 数据完整性修复（Neo4j schema/约束变更） |
| `BREAKING` | 不兼容的 API 或数据结构变更 |
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `perf` | 性能优化 |
| `refactor` | 架构调整（不改变外部行为） |
| `test` | 测试相关变更 |
| `deploy` | 部署/运维相关 |

## 变更记录格式

每条记录包含：
- **描述**：一句话说明做了什么
- **commit 链接**：关联到 GitHub commit
- **影响范围**：修改的模块路径
- **验证**：通过的测试/检查
- **部署状态**：`pending` / `deployed` / `rolled-back`
