#!/bin/bash
# 安装 Git Hooks（开发环境初始化时运行一次）
# 用法: ./setup-hooks.sh

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$REPO_ROOT/hooks"
GIT_HOOKS_DIR="$REPO_ROOT/.git/hooks"

echo "安装 Git Hooks..."

for hook in pre-commit pre-push; do
    if [ -f "$HOOKS_DIR/$hook" ]; then
        ln -sf "$HOOKS_DIR/$hook" "$GIT_HOOKS_DIR/$hook"
        echo "  ✓ $hook → .git/hooks/$hook"
    fi
done

echo "✅ Git Hooks 安装完成"
