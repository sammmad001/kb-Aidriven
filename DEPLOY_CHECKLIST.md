# 部署前检查清单

每次部署到 ECS 生产环境前，逐项确认以下检查点。

---

## 基本信息

| 项目 | 内容 |
|------|------|
| **部署日期** | ____年____月____日 |
| **变更描述** | _______________________________________________ |
| **变更类型** | □ bug修复  □ 功能优化  □ 架构调整  □ 配置变更 |
| **影响模块** | _______________________________________________ |

---

## 一、本地验证

- [ ] **1.1** `./pre-deploy-check.sh` 全部 PASS（无 FAIL 项）
- [ ] **1.2** 所有变更已 commit（`git status` 无未提交文件）
- [ ] **1.3** 已 push 到 GitHub（`git push origin main`）

---

## 二、变更记录

- [ ] **2.1** `CHANGELOG.md` 已更新（Unreleased 区域记录了本次变更）
- [ ] **2.2** 变更描述包含：影响范围、验证结果、部署状态

---

## 三、依赖与配置

- [ ] **3.1** `requirements.txt` 如有变更，已确认新依赖兼容性
- [ ] **3.2** `.env` 如有变更，已同步准备 ECS 端的更新方案
- [ ] **3.3** 新增的环境变量已在 ECS `.env` 中准备好

---

## 四、Neo4j 数据影响评估

> 仅在修改了以下文件时需要评估：`database.py`、`models.py`、`ingest/graph_process.py`、`ingest/analyze.py`

- [ ] **4.1** 是否修改了 MERGE/MATCH Cypher 语句？
  - 是 → 确认不会导致数据丢失或覆盖
- [ ] **4.2** 是否变更了 Schema 约束（constraint / index）？
  - 是 → 确认新约束与现有数据兼容
- [ ] **4.3** 是否修改了 `execute_write()` 的调用方式？
  - 是 → 确认参数传递正确
- [ ] **4.4** 是否涉及节点/边的删除操作？
  - 是 → 确认删除条件正确，不会误删数据

**评估结论**: □ 无数据影响  □ 低风险  □ 需备份后验证

---

## 五、备份确认

- [ ] **5.1** 当前 `/health` 返回 `status: ok`（基线确认）
- [ ] **5.2** 部署脚本会自动执行 `backup.sh`（或已手动备份）
- [ ] **5.3** 备份输出包含 `[OK]`（Neo4j dump / 素材 / 配置）

---

## 六、回滚准备

- [ ] **6.1** 知道回滚到哪个 commit（`git log --oneline -3`）
- [ ] **6.2** `rollback.sh` 脚本可用（`bash -n rollback.sh` 通过）
- [ ] **6.3** 如涉及数据变更，确认备份文件可恢复

---

## 七、部署后验证

部署完成后执行以下验证：

- [ ] **7.1** `curl http://43.106.12.79:8080/health` 返回 `status: ok`
- [ ] **7.2** 所有组件状态正常（neo4j / llm / feishu）
- [ ] **7.3** `journalctl -u knowledge-base -n 20` 无 ERROR 日志
- [ ] **7.4** 飞书 Bot 发送 `/stats` 验证功能正常
- [ ] **7.5** 更新 `CHANGELOG.md` 部署状态为 `deployed`

---

## 快速命令参考

```bash
# 本地验证
./pre-deploy-check.sh

# 部署
./deploy.sh

# 回滚（仅代码）
./rollback.sh --code-only

# 回滚（代码 + 数据）
./rollback.sh --with-data

# 查看 ECS 日志
ssh root@43.106.12.79 "journalctl -u knowledge-base -n 50"

# 手动备份
ssh root@43.106.12.79 "cd /opt/knowledge-base && bash backup.sh"

# 健康检查
curl http://43.106.12.79:8080/health
```

---

## 模块-验证映射

> 根据修改的模块，确定需要执行的验证项

| 修改模块 | 编译检查 | pytest | 前端构建 | Neo4j 影响 |
|----------|:--------:|:------:|:--------:|:----------:|
| `app/ingest/` | ✓ | test_ingest | - | **高** |
| `app/query/` | ✓ | test_query | - | 低 |
| `app/feishu/` | ✓ | test_feishu | - | 无 |
| `app/api/` | ✓ | 全部 | - | 中 |
| `app/models.py` | ✓ | 全部 | ✓ | **高** |
| `app/database.py` | ✓ | 全部 | - | **CRITICAL** |
| `app/config.py` | ✓ | 全部 | - | 低 |
| `app/lint/` | ✓ | - | - | 低 |
| `kb-web/` | - | - | ✓ | 无 |
