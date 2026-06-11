#!/bin/bash
# ============================================================
# Neo4j + 知识库数据备份脚本
# 使用方法: ./backup.sh [备份目录]
# 定时执行: crontab -e → 0 2 * * * /opt/knowledge-base/backup.sh
# ============================================================

set -e

BACKUP_DIR="${1:-/opt/knowledge-base/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=7

echo "===== 知识库备份开始: $TIMESTAMP ====="

# 创建备份目录
mkdir -p "$BACKUP_DIR/neo4j" "$BACKUP_DIR/raw" "$BACKUP_DIR/env"

# ------------------------------------------------------------------
# 1. Neo4j 数据库备份
# ------------------------------------------------------------------
echo "[1/3] 备份 Neo4j 数据库..."

NEO4J_HOME="${NEO4J_HOME:-/opt/neo4j}"
NEO4J_DUMP="$BACKUP_DIR/neo4j/kb_$TIMESTAMP.dump"

if command -v neo4j-admin &>/dev/null; then
    # Neo4j 5.x+ 使用 neo4j-admin database dump
    neo4j-admin database dump neo4j --to-path="$BACKUP_DIR/neo4j" 2>/dev/null && \
        mv "$BACKUP_DIR/neo4j/neo4j.dump" "$NEO4J_DUMP" 2>/dev/null || \
    # Fallback: 旧版命令
    neo4j-admin dump --database=neo4j --to="$NEO4J_DUMP" 2>/dev/null || \
    echo "  [WARN] neo4j-admin dump 失败，尝试文件级备份..."

    # Fallback: 直接复制数据目录
    if [ ! -f "$NEO4J_DUMP" ]; then
        DATA_DIR="$NEO4J_HOME/data/databases/neo4j"
        if [ -d "$DATA_DIR" ]; then
            tar -czf "$BACKUP_DIR/neo4j/kb_${TIMESTAMP}_data.tar.gz" -C "$DATA_DIR" . 2>/dev/null
            echo "  [OK] 文件级备份完成: kb_${TIMESTAMP}_data.tar.gz"
        else
            echo "  [ERROR] Neo4j 数据目录不存在: $DATA_DIR"
        fi
    else
        echo "  [OK] Neo4j dump 完成: $NEO4J_DUMP"
    fi
else
    echo "  [WARN] neo4j-admin 未找到，跳过数据库备份"
fi

# ------------------------------------------------------------------
# 2. 原始素材备份
# ------------------------------------------------------------------
echo "[2/3] 备份原始素材..."

RAW_DIR="/opt/knowledge-base/raw"
if [ -d "$RAW_DIR" ]; then
    tar -czf "$BACKUP_DIR/raw/raw_${TIMESTAMP}.tar.gz" -C "$RAW_DIR" . 2>/dev/null
    echo "  [OK] 素材备份完成: raw_${TIMESTAMP}.tar.gz"
else
    echo "  [SKIP] 素材目录不存在: $RAW_DIR"
fi

# ------------------------------------------------------------------
# 3. 环境配置备份
# ------------------------------------------------------------------
echo "[3/3] 备份环境配置..."

ENV_FILE="/opt/knowledge-base/.env"
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$BACKUP_DIR/env/.env_$TIMESTAMP"
    echo "  [OK] 配置备份完成"
else
    echo "  [SKIP] .env 文件不存在"
fi

# ------------------------------------------------------------------
# 清理过期备份
# ------------------------------------------------------------------
echo ""
echo "清理 $RETENTION_DAYS 天前的备份..."
DELETED=0

for dir in "$BACKUP_DIR/neo4j" "$BACKUP_DIR/raw" "$BACKUP_DIR/env"; do
    COUNT=$(find "$dir" -type f -mtime +$RETENTION_DAYS 2>/dev/null | wc -l)
    if [ "$COUNT" -gt 0 ]; then
        find "$dir" -type f -mtime +$RETENTION_DAYS -delete 2>/dev/null
        DELETED=$((DELETED + COUNT))
    fi
done

echo "  已清理 $DELETED 个过期备份文件"

# ------------------------------------------------------------------
# 汇总
# ------------------------------------------------------------------
BACKUP_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
echo ""
echo "===== 备份完成 ====="
echo "  备份目录: $BACKUP_DIR"
echo "  总大小: $BACKUP_SIZE"
echo "  保留策略: $RETENTION_DAYS 天"
echo "  时间戳: $TIMESTAMP"
echo "===================="
