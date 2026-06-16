# Changelog

本文件记录个人知识库系统的所有显著变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh/1.1.0/)。

---

## [Unreleased]

### fix
- 修复对话上下文时间戳碰撞+消息顺序错乱+研究测试mock ([v1.1.4])
  - 发布时间: 2026-06-16
  - 优先级: P0
  - 部署状态: pending

### fix
- 8维度审计修复: 安全加固/代码质量/功能补全/测试覆盖/lint清理 ([v1.1.1])
  - 发布时间: 2026-06-15
  - 优先级: P0
  - 部署状态: pending

### feat
- V1.1 知识推理优化 — 回退链+三级匹配+9类隐式关系+Reviewer+端到端验证 ([v1.1.0])
  - 发布时间: 2026-06-12
  - 优先级: P2
  - 部署状态: pending

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
