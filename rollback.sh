#!/bin/bash
# ============================================================
# 个人知识库一键回滚脚本
# 从 ECS 回滚到上一个版本
#
# 使用方法:
#   ./rollback.sh              # 交互式回滚
#   ./rollback.sh --code-only  # 仅回滚代码（不恢复数据）
#   ./rollback.sh --with-data  # 回滚代码 + 恢复 Neo4j 数据
#
# 回滚场景:
#   A. 代码回滚 — 应用层问题（API 报错、服务崩溃）
#   B. 数据回滚 — Neo4j 数据损坏（schema 变更导致数据丢失）
# ============================================================

set -e

# 配置
ECS_HOST="43.106.12.79"
ECS_USER="root"
ECS_SSH_KEY="$HOME/.ssh/id_ed25519"
ECS_APP_DIR="/opt/knowledge-base"
SSH_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# 参数解析
MODE="interactive"

for arg in "$@"; do
    case $arg in
        --code-only)
            MODE="code"
            ;;
        --with-data)
            MODE="data"
            ;;
        *)
            echo "未知参数: $arg"
            echo "用法: $0 [--code-only | --with-data]"
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ssh_cmd() {
    ssh $SSH_OPTS -i "$ECS_SSH_KEY" "$ECS_USER@$ECS_HOST" "$@"
}

echo ""
echo "============================================"
echo "  个人知识库回滚"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  目标: $ECS_USER@$ECS_HOST:$ECS_APP_DIR"
echo "============================================"
echo ""

# ------------------------------------------------------------------
# 1. 获取当前版本
# ------------------------------------------------------------------
echo -e "${BLUE}[1/6] 获取当前部署版本${NC}"

CURRENT_VERSION=$(ssh_cmd "cat $ECS_APP_DIR/.deploy_version 2>/dev/null || echo 'unknown'" 2>/dev/null || echo "unknown")
echo "  当前 ECS 版本: $CURRENT_VERSION"

if [ "$CURRENT_VERSION" = "unknown" ]; then
    echo -e "${YELLOW}⚠️  无法获取当前版本，将使用 Git 上一个 commit 作为回滚目标${NC}"
fi
echo ""

# ------------------------------------------------------------------
# 2. 确定回滚目标
# ------------------------------------------------------------------
echo -e "${BLUE}[2/6] 确定回滚目标${NC}"

cd "$SCRIPT_DIR" || exit 1

# 找到上一个 commit
if [ "$CURRENT_VERSION" != "unknown" ]; then
    # 找到当前版本的父 commit
    ROLLBACK_COMMIT=$(git log -1 --format="%h" "$CURRENT_VERSION"^ 2>/dev/null || echo "")
else
    # 使用上一个 commit
    ROLLBACK_COMMIT=$(git log -2 --format="%h" | tail -1)
fi

if [ -z "$ROLLBACK_COMMIT" ]; then
    echo -e "${RED}❌ 无法确定回滚目标（Git 历史不足）${NC}"
    exit 1
fi

ROLLBACK_MSG=$(git log -1 --format="%s" "$ROLLBACK_COMMIT" 2>/dev/null || echo "unknown")
echo "  回滚目标: $ROLLBACK_COMMIT"
echo "  描述:     $ROLLBACK_MSG"
echo ""

# ------------------------------------------------------------------
# 3. 确认回滚模式
# ------------------------------------------------------------------
echo -e "${BLUE}[3/6] 选择回滚模式${NC}"

if [ "$MODE" = "interactive" ]; then
    echo "  回滚模式:"
    echo "    1) 仅代码回滚（应用层问题）"
    echo "    2) 代码 + 数据回滚（Neo4j 数据损坏）"
    echo ""
    read -p "请选择 [1/2] (默认: 1): " CHOICE
    case $CHOICE in
        2)
            MODE="data"
            ;;
        *)
            MODE="code"
            ;;
    esac
fi

if [ "$MODE" = "data" ]; then
    echo -e "  模式: ${RED}代码 + 数据回滚${NC}"
    echo -e "  ${YELLOW}⚠️  这将恢复 Neo4j 备份，可能丢失最近的数据变更！${NC}"
else
    echo -e "  模式: ${YELLOW}仅代码回滚${NC}"
fi
echo ""

# ------------------------------------------------------------------
# 4. 确认回滚
# ------------------------------------------------------------------
echo -e "${BLUE}[4/6] 确认回滚操作${NC}"

echo ""
echo "============================================"
echo -e "  ${RED}即将执行回滚操作${NC}"
echo "============================================"
echo ""
echo "  当前版本: $CURRENT_VERSION"
echo "  回滚到:   $ROLLBACK_COMMIT ($ROLLBACK_MSG)"
echo "  模式:     $MODE"
echo ""
read -p "确认回滚？(y/N) " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "回滚已取消"
    exit 0
fi
echo ""

# ------------------------------------------------------------------
# 5. 执行回滚
# ------------------------------------------------------------------
echo -e "${BLUE}[5/6] 执行回滚${NC}"

# 5a. 数据回滚（如需要）
if [ "$MODE" = "data" ]; then
    echo "  [5a] 恢复 Neo4j 数据..."
    
    # 停止服务
    echo "  停止服务..."
    ssh_cmd "systemctl stop knowledge-base" || true
    sleep 2
    
    # 找到最新的备份
    LATEST_BACKUP=$(ssh_cmd "ls -t $ECS_APP_DIR/backups/neo4j/*.dump 2>/dev/null | head -1" 2>/dev/null || echo "")
    
    if [ -n "$LATEST_BACKUP" ]; then
        echo "  找到备份: $LATEST_BACKUP"
        echo "  正在恢复 Neo4j 数据..."
        
        # 恢复数据
        ssh_cmd "neo4j-admin database load neo4j --from-path=$(dirname "$LATEST_BACKUP") --overwrite-destination" 2>&1 || {
            echo -e "  ${YELLOW}⚠️  neo4j-admin load 失败，尝试文件级恢复...${NC}"
        }
        
        echo -e "  ${GREEN}✓${NC} Neo4j 数据恢复完成"
    else
        echo -e "  ${YELLOW}⚠️  未找到 Neo4j 备份文件，跳过数据恢复${NC}"
    fi
fi

# 5b. 代码回滚
echo "  [5b] 回滚代码..."

# 切换到回滚版本的代码
git checkout "$ROLLBACK_COMMIT" -- kb/ 2>/dev/null || {
    echo -e "${RED}❌ 无法切换到 commit $ROLLBACK_COMMIT${NC}"
    exit 1
}

# 上传回滚版本的代码
echo "  上传代码到 ECS..."
rsync -avz --delete \
    --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' --exclude='.env' \
    -e "ssh $SSH_OPTS -i $ECS_SSH_KEY" \
    "$SCRIPT_DIR/kb/" "$ECS_USER@$ECS_APP_DIR/" 2>&1 | tail -3

# 恢复本地代码到最新状态（回滚只是部署操作，不改变本地开发状态）
git checkout HEAD -- kb/ 2>/dev/null || true

echo -e "  ${GREEN}✓${NC} 代码回滚完成"
echo ""

# ------------------------------------------------------------------
# 6. 重启并验证
# ------------------------------------------------------------------
echo -e "${BLUE}[6/6] 重启服务并验证${NC}"

# 启动服务
if [ "$MODE" = "data" ]; then
    # 数据恢复后需要重启 Neo4j
    echo "  重启 Neo4j..."
    ssh_cmd "neo4j restart" 2>/dev/null || ssh_cmd "systemctl restart neo4j" 2>/dev/null || true
    sleep 5
fi

echo "  重启 Knowledge API..."
ssh_cmd "systemctl restart knowledge-base"
echo "  等待服务启动 (5秒)..."
sleep 5

# 健康检查
HEALTH=$(ssh_cmd "curl -s http://localhost:8080/health" 2>/dev/null || echo '{"status":"error"}')
STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")

if [ "$STATUS" = "ok" ]; then
    echo -e "  ${GREEN}✓${NC} 健康检查通过"
else
    echo -e "  ${RED}❌ 健康检查失败: $STATUS${NC}"
fi
echo ""

# ------------------------------------------------------------------
# 回滚结果
# ------------------------------------------------------------------
echo "============================================"
if [ "$STATUS" = "ok" ]; then
    echo -e "  ${GREEN}✅ 回滚成功${NC}"
else
    echo -e "  ${RED}⚠️  回滚完成（服务状态异常）${NC}"
fi
echo "============================================"
echo ""
echo "  回滚到: $ROLLBACK_COMMIT"
echo "  描述:   $ROLLBACK_MSG"
echo "  模式:   $MODE"
echo "  时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
echo "  下一步:"
echo "    1. 检查服务日志: ssh $ECS_USER@$ECS_HOST 'journalctl -u knowledge-base -n 50'"
echo "    2. 更新 CHANGELOG.md 标记 rolled-back"
echo "    3. 修复问题后重新部署"
echo ""
