# 部署工作流规则（Always）

## 适用范围

任何涉及 `kb/` 或 `kb-web/` 目录的代码变更完成时，必须执行完整发布流程。

## 触发时机（Always）

每次完成代码修改后，**不允许**只修改代码而不走发布流程。

---

## 完整工作流（4 步）

### 步骤 1：Git 提交

```
git add <changed files>
git commit -m "<type>(<scope>): <description>"
```

- 提交信息遵循 conventional commits 格式
- pre-commit hook 自动执行：Python 编译检查 + pytest（仅 kb/ 变更时）

### 步骤 2：Git 推送

```
git push origin main
```

- pre-push hook 自动执行：完整 5 项验证（编译、导入、49 tests、TS 类型、前端构建）
- 5 项全部通过才允许推送

### 步骤 3：ECS 部署

```bash
cd /Users/sam/Desktop/个人知识库 && echo "y" | bash deploy.sh --skip-backup
```

部署流程（8 步）：
1. 检查 Git 工作区（有未提交变更会警告但继续）
2. 检查 ECS 连通性（SSH 43.106.12.79）
3. 备份（--skip-backup 跳过）
4. rsync 上传 kb/ 目录到 ECS /opt/knowledge-base/
5. 安装 Python 依赖（pip install -r requirements.txt）
6. 重启服务（systemctl restart knowledge-base）
7. 健康检查（curl /health → neo4j/llm/feishu/scheduler 四个组件）
8. 检查服务日志（ERROR 日志检测）

### 步骤 4：部署后验证

部署完成后必须验证三项：

```bash
# 健康检查
ssh root@43.106.12.79 "curl -s http://localhost:8080/health"

# 入库状态
ssh root@43.106.12.79 "curl -s -H 'Authorization: Bearer dev-token' http://localhost:8080/api/ingest/status"

# 图谱统计
ssh root@43.106.12.79 "curl -s -H 'Authorization: Bearer dev-token' http://localhost:8080/api/graph/stats"
```

**验证标准**：
- 健康检查：4 个组件均 `ok`
- 入库状态：`failed: 0`
- 图谱统计：节点/关系数符合预期

### 前端（可选）

如果修改了 `kb-web/` 文件，需要重启前端 dev server：

```bash
pkill -f "vite" && cd /Users/sam/Desktop/个人知识库/kb-web && npm run dev
```

验证前端代理正常：
```bash
curl -s http://localhost:5174/api/graph/stats
```

---

## 关键配置

| 配置项 | 值 |
|--------|-----|
| ECS Host | 43.106.12.79 |
| SSH Key | ~/.ssh/id_ed25519 |
| SSH User | root |
| 部署目录 | /opt/knowledge-base |
| 部署脚本 | /Users/sam/Desktop/个人知识库/deploy.sh |
| API Token | dev-token |

---

## 禁止事项

- ❌ 直接 scp 文件到 ECS，绕过 deploy.sh
- ❌ 修改代码后不提交、不推送
- ❌ 跳过健康检查直接结束
- ❌ 部署失败后不排查直接忽略
