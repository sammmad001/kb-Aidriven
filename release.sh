#!/bin/bash
# ============================================================
# 个人知识库标准化发布脚本
# 8 步流程：验证 → 版本 → CHANGELOG → commit → tag → push
# Push 后 GitHub CD 自动部署到 ECS
#
# 使用方法:
#   bash release.sh <version> <priority> "<commit_message>"
#   bash release.sh 1.0.2 P1 "修复实体匹配bug"
#
# Priority: P0/P1/P2/P3
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 参数解析 ──────────────────────────────────────────────
VERSION="${1:-}"
PRIORITY="${2:-}"
MESSAGE="${3:-}"

if [ -z "$VERSION" ] || [ -z "$PRIORITY" ] || [ -z "$MESSAGE" ]; then
    echo "用法: bash release.sh <version> <priority> \"<commit_message>\""
    echo "示例: bash release.sh 1.0.2 P1 \"修复实体匹配bug\""
    exit 1
fi

# 验证 priority
case "$PRIORITY" in
    P0|P1|P2|P3) ;;
    *)
        echo "错误: priority 必须是 P0/P1/P2/P3"
        exit 1
        ;;
esac

# ── 确定 CHANGELOG 分类 ──────────────────────────────────
case "$PRIORITY" in
    P0|P1) CHANGELOG_SECTION="fix" ;;
    P2)    CHANGELOG_SECTION="feat" ;;
    P3)    CHANGELOG_SECTION="refactor" ;;
esac

TIMESTAMP=$(date '+%Y-%m-%d')
COMMIT_MSG="release: v${VERSION} [${PRIORITY}] ${MESSAGE}"
TAG_MSG="v${VERSION}: ${MESSAGE}"

echo "============================================"
echo "  个人知识库 发布流程"
echo "============================================"
echo "  版本:    v${VERSION}"
echo "  优先级:  ${PRIORITY}"
echo "  消息:    ${MESSAGE}"
echo "  Commit:  ${COMMIT_MSG}"
echo "============================================"
echo ""

# ── [1/8] 部署前验证 ─────────────────────────────────────
echo -e "\033[1;34m[1/8] 部署前验证 (pre-deploy-check.sh)\033[0m"
if [ -f "pre-deploy-check.sh" ]; then
    bash pre-deploy-check.sh
    echo "✅ 验证通过"
else
    echo "⚠️  pre-deploy-check.sh 不存在，跳过"
fi
echo ""

# ── [2/8] 获取当前版本 ───────────────────────────────────
echo -e "\033[1;34m[2/8] 获取当前版本号\033[0m"
CURRENT_TAG=$(git tag --sort=-v:refname 2>/dev/null | grep -E '^v[0-9]' | head -1)
if [ -z "$CURRENT_TAG" ]; then
    echo "  当前版本: 无 (首次发布)"
else
    echo "  当前版本: ${CURRENT_TAG}"
fi
echo "  新版本:   v${VERSION}"
echo ""

# ── [3/8] 更新 CHANGELOG ─────────────────────────────────
echo -e "\033[1;34m[3/8] 更新 CHANGELOG.md\033[0m"
if [ -f "CHANGELOG.md" ]; then
    # 在 [Unreleased] 和 --- 之间插入新条目
    CHANGELOG_ENTRY="### ${CHANGELOG_SECTION}
- ${MESSAGE} ([v${VERSION}])
  - 发布时间: ${TIMESTAMP}
  - 优先级: ${PRIORITY}
  - 部署状态: pending
"

    # 使用 sed 在 [Unreleased] 段落的开头插入新条目
    # macOS sed 兼容
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "/^## \[Unreleased\]/,/^---/{ /^_暂无未发布变更_/{
            s/^_暂无未发布变更_$//
            a\\
${CHANGELOG_ENTRY}
        }}" CHANGELOG.md
    else
        sed -i "/^## \[Unreleased\]/,/^---/{ /^_暂无未发布变更_/{
            s/^_暂无未发布变更_$//
            a\\
${CHANGELOG_ENTRY}
        }}" CHANGELOG.md
    fi
    echo "✅ CHANGELOG.md 已更新"
else
    echo "⚠️  CHANGELOG.md 不存在，跳过"
fi
echo ""

# ── [4/8] 展示变更摘要 ───────────────────────────────────
echo -e "\033[1;34m[4/8] 变更摘要\033[0m"
echo ""
echo "--- git status ---"
git status --short
echo ""
echo "--- git diff --stat ---"
git diff --stat --cached 2>/dev/null || git diff --stat
echo ""

# ── [5/8] 用户二次确认 ───────────────────────────────────
if [ "$PRIORITY" = "P0" ]; then
    echo -e "\033[1;31m╔══════════════════════════════════════════════╗\033[0m"
    echo -e "\033[1;31m║  ⚠️  紧急修复 (P0) — 将立即推送到 main       ║\033[0m"
    echo -e "\033[1;31m║  此操作触发 ECS 自动部署，请仔细确认 !!       ║\033[0m"
    echo -e "\033[1;31m╚══════════════════════════════════════════════╝\033[0m"
    echo ""
    read -p "确认发布 v${VERSION}? (输入 yes 确认): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo "❌ 已取消"
        exit 0
    fi
else
    echo -e "\033[1;33m确认发布 v${VERSION}? [y/N]:\033[0m "
    read -r CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "yes" ]; then
        echo "❌ 已取消"
        exit 0
    fi
fi
echo ""

# ── [6/8] Git Add ────────────────────────────────────────
echo -e "\033[1;34m[6/8] git add -A\033[0m"
git add -A
echo "✅ 已暂存"
echo ""

# ── [7/8] Git Commit ─────────────────────────────────────
echo -e "\033[1;34m[7/8] git commit\033[0m"
git commit -m "${COMMIT_MSG}"
echo "✅ 已提交"
echo ""

# ── [8/8] Git Tag + Push ─────────────────────────────────
echo -e "\033[1;34m[8/8] git tag + push\033[0m"
git tag -a "v${VERSION}" -m "${TAG_MSG}"
echo "✅ 已打标签 v${VERSION}"

echo "推送中..."
git push origin main
git push --tags
echo "✅ 推送完成"
echo ""

echo "============================================"
echo "  🚀 发布完成！v${VERSION}"
echo "============================================"
echo "  GitHub Actions 将自动部署到 ECS"
echo "  查看进度: https://github.com/sammmad001/kb-Aidriven/actions"
echo "============================================"
