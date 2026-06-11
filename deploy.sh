#!/bin/bash
# ============================================================
# 个人知识库一键部署脚本
# 从本地 Mac 部署到阿里云 ECS
#
# 使用方法:
#   ./deploy.sh              # 标准部署（含备份）
#   ./deploy.sh --skip-backup # 跳过备份（仅紧急热修复）
#   ./deploy.sh --dry-run    # 仅输出步骤，不实际执行
#
# 前置条件:
#   1. pre-deploy-check.sh 已通过
#   2. 所有变更已 commit
# ============================================================

set -e

# 配置
ECS_HOST="43.106.12.79"
ECS_USER="root"
ECS_SSH_KEY="$HOME/.ssh/id_ed25519"
ECS_APP_DIR="/opt/knowledge-base"
SSH_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# 参数解析
SKIP_BACKUP=false
DRY_RUN=false

for arg in "$@"; do
    case $arg in
        --skip-backup)
            SKIP_BACKUP=true
            echo "⚠️  跳过备份模式（仅用于紧急热修复）"
            ;;
        --dry-run)
            DRY_RUN=true
            echo "🔍 干跑模式（仅输出步骤，不实际执行）"
            ;;
        *)
            echo "未知参数: $arg"
            echo "用法: $0 [--skip-backup] [--dry-run]"
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_DIR="$SCRIPT_DIR/kb"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ssh_cmd() {
    if [ "$DRY_RUN" = true ]; then
        echo -e "  ${BLUE}[DRY-RUN]${NC} ssh $ECS_USER@$ECS_HOST: $*"
    else
        ssh $SSH_OPTS -i "$ECS_SSH_KEY" "$ECS_USER@$ECS_HOST" "$@"
    fi
}

scp_cmd() {
    if [ "$DRY_RUN" = true ]; then
        echo -e "  ${BLUE}[DRY-RUN]${NC} scp $1 -> $2"
    else
        scp $SSH_OPTS -i "$ECS_SSH_KEY" -r "$1" "$2"
    fi
}

echo ""
echo "============================================"
echo "  个人知识库部署"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  目标: $ECS_USER@$ECS_HOST:$ECS_APP_DIR"
echo "============================================"
echo ""

# ------------------------------------------------------------------
# 1. 检查 Git 工作区
# ------------------------------------------------------------------
echo -e "${BLUE}[1/8] 检查 Git 工作区${NC}"

cd "$SCRIPT_DIR" || exit 1

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    UNCOMMITTED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    if [ "$UNCOMMITTED" -gt 0 ]; then
        echo -e "${YELLOW}⚠️  警告: 有 $UNCOMMITTED 个未提交变更${NC}"
        if [ "$DRY_RUN" = false ]; then
            read -p "是否继续部署？(y/N) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                echo "部署已取消"
                exit 0
            fi
        fi
    fi
fi

CURRENT_COMMIT=$(git log -1 --format="%h" 2>/dev/null || echo "unknown")
COMMIT_MSG=$(git log -1 --format="%s" 2>/dev/null || echo "unknown")
echo -e "  版本: $CURRENT_COMMIT"
echo -e "  描述: $COMMIT_MSG"
echo ""

# ------------------------------------------------------------------
# 2. 检查 ECS 连通性
# ------------------------------------------------------------------
echo -e "${BLUE}[2/8] 检查 ECS 连通性${NC}"

if [ "$DRY_RUN" = true ]; then
    echo -e "  ${BLUE}[DRY-RUN]${NC} 检查 SSH 连接..."
else
    if ! ssh $SSH_OPTS -i "$ECS_SSH_KEY" "$ECS_USER@$ECS_HOST" "echo 'SSH OK'" >/dev/null 2>&1; then
        echo -e "${RED}❌ 无法连接到 ECS ($ECS_HOST)${NC}"
        echo "  请检查:"
        echo "  - SSH 密钥: $ECS_SSH_KEY"
        echo "  - ECS IP: $ECS_HOST"
        echo "  - 网络连通性"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} ECS 连接正常"
fi
echo ""

# ------------------------------------------------------------------
# 3. 备份（可选）
# ------------------------------------------------------------------
echo -e "${BLUE}[3/8] ECS 数据备份${NC}"

if [ "$SKIP_BACKUP" = true ]; then
    echo -e "  ${YELLOW}⚠️  跳过备份（--skip-backup 模式）${NC}"
else
    echo "  正在执行 backup.sh..."
    if ssh_cmd "cd $ECS_APP_DIR && bash backup.sh 2>&1 | tail -5"; then
        echo -e "  ${GREEN}✓${NC} 备份完成"
    else
        echo -e "${RED}❌ 备份失败，中止部署${NC}"
        echo "  安全第一：备份失败时不允许部署"
        echo "  如需强制部署，使用: $0 --skip-backup"
        exit 1
    fi
fi
echo ""

# ------------------------------------------------------------------
# 4. 上传代码
# ------------------------------------------------------------------
echo -e "${BLUE}[4/8] 上传代码到 ECS${NC}"

# 排除不需要同步的目录
EXCLUDES="--exclude=__pycache__ --exclude=.pytest_cache --exclude=*.pyc --exclude=.env"

if [ "$DRY_RUN" = true ]; then
    scp_cmd "$KB_DIR/*" "$ECS_USER@$ECS_HOST:$ECS_APP_DIR/"
else
    echo "  正在同步 kb/ -> ECS:/opt/knowledge-base/ ..."
    rsync -avz --delete $EXCLUDES \
        -e "ssh $SSH_OPTS -i $ECS_SSH_KEY" \
        "$KB_DIR/" "$ECS_USER@$ECS_APP_DIR/" 2>&1 | tail -5
    echo -e "  ${GREEN}✓${NC} 代码上传完成"
fi
echo ""

# ------------------------------------------------------------------
# 5. 检查依赖变更
# ------------------------------------------------------------------
echo -e "${BLUE}[5/8] 检查依赖变更${NC}"

# 检查 requirements.txt 是否有变化（通过比较文件 hash）
if [ "$DRY_RUN" = true ]; then
    ssh_cmd "cd $ECS_APP_DIR && source venv/bin/activate && pip install -r requirements.txt"
else
    # 始终执行 pip install -r 以确保依赖一致
    echo "  安装 Python 依赖..."
    ssh_cmd "cd $ECS_APP_DIR && source venv/bin/activate && pip install -q -r requirements.txt" 2>&1 | tail -3
    echo -e "  ${GREEN}✓${NC} 依赖安装完成"
fi
echo ""

# ------------------------------------------------------------------
# 6. 重启服务
# ------------------------------------------------------------------
echo -e "${BLUE}[6/8] 重启服务${NC}"

ssh_cmd "systemctl restart knowledge-base"
echo "  等待服务启动 (5秒)..."
if [ "$DRY_RUN" = false ]; then
    sleep 5
fi
echo ""

# ------------------------------------------------------------------
# 7. 健康检查
# ------------------------------------------------------------------
echo -e "${BLUE}[7/8] 健康检查${NC}"

if [ "$DRY_RUN" = true ]; then
    ssh_cmd "curl -s http://localhost:8080/health"
else
    HEALTH=$(ssh_cmd "curl -s http://localhost:8080/health" 2>/dev/null || echo '{"status":"error"}')
    
    # 解析健康状态
    STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    
    if [ "$STATUS" = "ok" ]; then
        echo -e "  ${GREEN}✓${NC} 健康检查通过"
        echo "  组件状态:"
        echo "$HEALTH" | python3 -c "
import sys, json
h = json.load(sys.stdin)
for k, v in h.get('components', {}).items():
    print(f'    {k}: {v}')
" 2>/dev/null
    else
        echo -e "  ${RED}❌ 健康检查失败: $STATUS${NC}"
        echo "  响应: $HEALTH"
        echo ""
        echo -e "${RED}============================================${NC}"
        echo -e "${RED}  部署可能失败！建议执行回滚:${NC}"
        echo -e "${RED}  ./rollback.sh${NC}"
        echo -e "${RED}============================================${NC}"
        exit 1
    fi
fi
echo ""

# ------------------------------------------------------------------
# 8. 检查日志错误
# ------------------------------------------------------------------
echo -e "${BLUE}[8/8] 检查服务日志${NC}"

if [ "$DRY_RUN" = true ]; then
    ssh_cmd "journalctl -u knowledge-base --no-pager -n 10 --since '1 min ago'"
else
    ERROR_COUNT=$(ssh_cmd "journalctl -u knowledge-base --no-pager -n 20 --since '1 min ago' | grep -c 'ERROR' || echo 0" 2>/dev/null || echo "0")
    
    if [ "$ERROR_COUNT" -gt 0 ]; then
        echo -e "  ${YELLOW}⚠️  发现 $ERROR_COUNT 条 ERROR 日志${NC}"
        ssh_cmd "journalctl -u knowledge-base --no-pager -n 20 --since '1 min ago' | grep 'ERROR'" 2>/dev/null || true
    else
        echo -e "  ${GREEN}✓${NC} 无 ERROR 日志"
    fi
fi
echo ""

# ------------------------------------------------------------------
# 写入版本标记
# ------------------------------------------------------------------
if [ "$DRY_RUN" = false ]; then
    ssh_cmd "echo '$CURRENT_COMMIT' > $ECS_APP_DIR/.deploy_version"
fi

# ------------------------------------------------------------------
# 部署摘要
# ------------------------------------------------------------------
echo "============================================"
echo -e "  ${GREEN}✅ 部署完成${NC}"
echo "============================================"
echo ""
echo "  版本:     $CURRENT_COMMIT"
echo "  描述:     $COMMIT_MSG"
echo "  目标:     $ECS_USER@$ECS_HOST"
echo "  时间:     $(date '+%Y-%m-%d %H:%M:%S')"
echo "  健康检查: http://$ECS_HOST:8080/health"
echo ""
echo "  下一步:"
echo "    1. 更新 CHANGELOG.md 部署状态为 deployed"
echo "    2. 飞书 Bot 发送 /stats 验证功能"
echo "    3. git push origin main（如未推送）"
echo ""
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}（干跑模式，未实际执行任何操作）${NC}"
    echo ""
fi
