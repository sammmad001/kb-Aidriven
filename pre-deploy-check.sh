#!/bin/bash
# ============================================================
# 部署前本地验证脚本
# 使用方法: ./pre-deploy-check.sh
# 退出码: 0=全部通过, 1=有失败
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_DIR="$SCRIPT_DIR/kb"
WEB_DIR="$SCRIPT_DIR/kb-web"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 计数器
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

# 结果记录
declare -a RESULTS=()

log_pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    RESULTS+=("  ${GREEN}✓ PASS${NC}  $1")
    echo -e "  ${GREEN}✓ PASS${NC}  $1"
}

log_fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    RESULTS+=("  ${RED}✗ FAIL${NC}  $1")
    echo -e "  ${RED}✗ FAIL${NC}  $1"
}

log_warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    RESULTS+=("  ${YELLOW}⚠ WARN${NC}  $1")
    echo -e "  ${YELLOW}⚠ WARN${NC}  $1"
}

log_info() {
    echo -e "  ${BLUE}→${NC} $1"
}

echo ""
echo "============================================"
echo "  部署前本地验证"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
echo ""

# ------------------------------------------------------------------
# 1. Python 编译检查
# ------------------------------------------------------------------
echo -e "${BLUE}[1/6] Python 编译检查${NC}"

if [ ! -d "$KB_DIR/app" ]; then
    log_fail "kb/app 目录不存在"
else
    COMPILE_ERRORS=0
    TOTAL_FILES=0
    
    while IFS= read -r -d '' pyfile; do
        TOTAL_FILES=$((TOTAL_FILES + 1))
        if ! python3 -m py_compile "$pyfile" 2>/dev/null; then
            COMPILE_ERRORS=$((COMPILE_ERRORS + 1))
            log_fail "编译失败: $(basename "$pyfile")"
        fi
    done < <(find "$KB_DIR/app" -name "*.py" -print0)
    
    if [ $COMPILE_ERRORS -eq 0 ]; then
        log_pass "Python 编译检查 ($TOTAL_FILES 个文件)"
    fi
fi

echo ""

# ------------------------------------------------------------------
# 2. 应用导入检查
# ------------------------------------------------------------------
echo -e "${BLUE}[2/6] 应用导入检查${NC}"

cd "$KB_DIR" || exit 1
if python3 -c "from app.main import app" 2>/dev/null; then
    log_pass "FastAPI 应用导入成功"
else
    log_fail "FastAPI 应用导入失败"
fi

echo ""

# ------------------------------------------------------------------
# 3. pytest 全量测试
# ------------------------------------------------------------------
echo -e "${BLUE}[3/6] pytest 测试套件${NC}"

cd "$KB_DIR" || exit 1
TEST_OUTPUT=$(python3 -m pytest tests/ -x -q --tb=short 2>&1)
TEST_EXIT=$?

if [ $TEST_EXIT -eq 0 ]; then
    # 提取测试数量
    TEST_COUNT=$(echo "$TEST_OUTPUT" | tail -1)
    log_pass "pytest: $TEST_COUNT"
else
    log_fail "pytest 测试失败"
    echo "$TEST_OUTPUT" | tail -10
fi

echo ""

# ------------------------------------------------------------------
# 4. 前端类型检查
# ------------------------------------------------------------------
echo -e "${BLUE}[4/6] 前端 TypeScript 类型检查${NC}"

if [ ! -d "$WEB_DIR" ]; then
    log_warn "kb-web 目录不存在，跳过前端检查"
else
    cd "$WEB_DIR" || exit 1
    
    if [ ! -d "node_modules" ]; then
        log_warn "node_modules 不存在，跳过前端检查（先运行 npm install）"
    else
        if npx tsc -b --noEmit 2>/dev/null; then
            log_pass "TypeScript 类型检查"
        else
            log_fail "TypeScript 类型检查失败"
        fi
    fi
fi

echo ""

# ------------------------------------------------------------------
# 5. 前端构建验证
# ------------------------------------------------------------------
echo -e "${BLUE}[5/6] 前端构建验证${NC}"

if [ ! -d "$WEB_DIR" ]; then
    log_warn "kb-web 目录不存在，跳过前端构建"
else
    cd "$WEB_DIR" || exit 1
    
    if [ ! -d "node_modules" ]; then
        log_warn "node_modules 不存在，跳过前端构建"
    else
        BUILD_OUTPUT=$(npm run build 2>&1)
        BUILD_EXIT=$?
        
        if [ $BUILD_EXIT -eq 0 ]; then
            log_pass "前端构建成功"
        else
            log_fail "前端构建失败"
            echo "$BUILD_OUTPUT" | tail -5
        fi
    fi
fi

echo ""

# ------------------------------------------------------------------
# 6. Git 工作区检查
# ------------------------------------------------------------------
echo -e "${BLUE}[6/6] Git 工作区状态${NC}"

cd "$SCRIPT_DIR" || exit 1

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    UNCOMMITTED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    
    if [ "$UNCOMMITTED" -eq 0 ]; then
        log_pass "Git 工作区干净"
    else
        log_warn "Git 工作区有 $UNCOMMITTED 个未提交变更"
        echo ""
        log_info "未提交的文件:"
        git status --porcelain 2>/dev/null | head -10 | while read -r line; do
            echo "      $line"
        done
    fi
    
    # 显示当前分支和最新 commit
    CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
    LATEST_COMMIT=$(git log -1 --format="%h %s" 2>/dev/null || echo "unknown")
    log_info "分支: $CURRENT_BRANCH | 最新: $LATEST_COMMIT"
else
    log_warn "不在 Git 仓库中"
fi

echo ""

# ------------------------------------------------------------------
# 汇总报告
# ------------------------------------------------------------------
echo "============================================"
echo "  验证汇总"
echo "============================================"
echo ""

for result in "${RESULTS[@]}"; do
    echo -e "$result"
done

echo ""
echo "--------------------------------------------"
echo -e "  ${GREEN}通过: $PASS_COUNT${NC}  ${RED}失败: $FAIL_COUNT${NC}  ${YELLOW}警告: $WARN_COUNT${NC}"
echo "--------------------------------------------"

if [ $FAIL_COUNT -gt 0 ]; then
    echo ""
    echo -e "${RED}❌ 验证未通过，请修复上述问题后再部署${NC}"
    echo ""
    exit 1
else
    echo ""
    if [ $WARN_COUNT -gt 0 ]; then
        echo -e "${YELLOW}⚠️  验证通过（有警告），可以继续部署${NC}"
    else
        echo -e "${GREEN}✅ 全部验证通过，可以部署${NC}"
    fi
    echo ""
    exit 0
fi
