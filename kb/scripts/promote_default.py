#!/usr/bin/env python3
"""将 default 服务账户升级为个人账户，归并飞书数据，清理测试账户。

一次性运维脚本。在 ECS 服务器上执行：

    cd /opt/knowledge-base
    source venv/bin/activate
    python scripts/promote_default.py --username <用户名> --password <密码>

操作内容：
  1. 升级 default 账户（改用户名+密码+取消 service 标记）
  2. 更新飞书映射指向 default
  3. 清理测试账户（verify_0616, testuser, feishu_ou_269c6a084）
  4. Neo4j 数据归并（将旧 feishu 账户的 2 个节点迁移到 default）
  5. 打印最终状态验证
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# 确保能 import app.auth.password
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth.password import hash_password
from neo4j import GraphDatabase

# ── 常量 ──────────────────────────────────────────────────────────────

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "samyezi960221")
DB_PATH = os.environ.get("USER_DB_PATH", "data/users.db")
DEFAULT_USER_ID = "default"

# 用户的飞书 open_id（从数据库查询确认）
FEISHU_OPEN_ID = "ou_269c6a084b66e197b695e5bf29943698"

# 待清理的测试 / 旧账户
CLEANUP_USER_IDS = [
    "usr_2026061515590806",   # verify_0616
    "usr_2026061605442344",   # testuser
    "usr_2026061604014238",   # feishu_ou_269c6a084（旧自动创建）
]

# 旧 feishu 账户的 user_id（用于 Neo4j 迁移）
OLD_FEISHU_USER_ID = "usr_2026061604014238"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将 default 账户升级为个人账户",
    )
    parser.add_argument("--username", required=True, help="新用户名（3-50字符）")
    parser.add_argument("--password", required=True, help="密码（≥6字符）")
    args = parser.parse_args()

    # ── 前置校验 ──────────────────────────────────────────────────────
    import re

    if not re.match(r"^[a-zA-Z0-9_\-]{3,50}$", args.username):
        print(f"❌ 用户名格式无效: {args.username}")
        print("   规则: 3-50字符，仅允许 [a-zA-Z0-9_-]")
        sys.exit(1)

    if len(args.password) < 6:
        print("❌ 密码长度不足，至少 6 个字符")
        sys.exit(1)

    print("=" * 60)
    print("  default 账户升级脚本")
    print("=" * 60)
    print(f"  目标用户名: {args.username}")
    print(f"  SQLite 路径: {DB_PATH}")
    print(f"  Neo4j URI:   {NEO4J_URI}")
    print()

    # ── Step 1: 升级 default 账户 ────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 确认 default 账户存在
    row = conn.execute("SELECT * FROM users WHERE id = ?", (DEFAULT_USER_ID,)).fetchone()
    if not row:
        print(f"❌ 未找到 id='default' 的用户，终止")
        sys.exit(1)
    print(f"[旧] id={row['id']}, username={row['username']}, is_service={row['is_service']}")

    pwd_hash = hash_password(args.password)
    conn.execute(
        "UPDATE users SET username = ?, password_hash = ?, is_service = 0 "
        "WHERE id = ?",
        (args.username, pwd_hash, DEFAULT_USER_ID),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM users WHERE id = ?", (DEFAULT_USER_ID,)).fetchone()
    print(f"[新] id={row['id']}, username={row['username']}, is_service={row['is_service']}")
    print("[1/5] ✅ default 账户已升级")
    print()

    # ── Step 2: 更新飞书映射 ──────────────────────────────────────────
    result = conn.execute(
        "UPDATE feishu_user_mappings SET user_id = ? WHERE open_id = ?",
        (DEFAULT_USER_ID, FEISHU_OPEN_ID),
    )
    conn.commit()
    updated = result.rowcount
    print(f"[2/5] ✅ 飞书映射已更新: {FEISHU_OPEN_ID} -> {DEFAULT_USER_ID} (影响 {updated} 行)")
    print()

    # ── Step 3: 清理测试账户 ──────────────────────────────────────────
    for uid in CLEANUP_USER_IDS:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            name = row["username"]
            conn.execute("DELETE FROM feishu_user_mappings WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            print(f"       已删除: id={uid}, username={name}")
        else:
            print(f"       跳过（不存在）: id={uid}")
    conn.commit()
    print("[3/5] ✅ 测试账户已清理")
    print()

    # ── Step 4: Neo4j 数据归并 ────────────────────────────────────────
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        # 迁移旧 feishu 账户的节点
        result = session.run(
            "MATCH (n) WHERE n.user_id = $old_uid "
            "SET n.user_id = $new_uid "
            "RETURN count(n) AS migrated",
            old_uid=OLD_FEISHU_USER_ID,
            new_uid=DEFAULT_USER_ID,
        )
        migrated = result.single()["migrated"]
        print(f"[4/5] ✅ Neo4j 已迁移 {migrated} 个节点: {OLD_FEISHU_USER_ID} -> {DEFAULT_USER_ID}")
        print()

        # ── Step 5: 验证最终状态 ──────────────────────────────────────
        print("[5/5] 最终状态验证:")
        print()

        # SQLite
        print("  ── SQLite users 表 ──")
        for r in conn.execute("SELECT id, username, is_service FROM users").fetchall():
            print(f"     id={r['id']:45s}  username={r['username']:20s}  is_service={r['is_service']}")

        print()
        print("  ── SQLite feishu_user_mappings 表 ──")
        for r in conn.execute("SELECT * FROM feishu_user_mappings").fetchall():
            print(f"     open_id={r['open_id']}  user_id={r['user_id']}")

        print()
        print("  ── Neo4j 节点分布 ──")
        result = session.run(
            "MATCH (n) WHERE n.user_id IS NOT NULL "
            "RETURN n.user_id AS uid, count(n) AS cnt "
            "ORDER BY cnt DESC"
        )
        total = 0
        for r in result:
            cnt = r["cnt"]
            total += cnt
            print(f"     user_id={r['uid']:45s}  nodes={cnt}")
        print(f"     {'TOTAL':52s}  nodes={total}")

    conn.close()
    driver.close()

    print()
    print("=" * 60)
    print(f"  ✅ 升级完成！")
    print(f"  用户名: {args.username}")
    print(f"  user_id: {DEFAULT_USER_ID}")
    print(f"  飞书已绑定: {FEISHU_OPEN_ID}")
    print(f"  总节点数: {total}")
    print(f"  现在可以重启服务并通过飞书 /whoami 或 Web 端登录验证")
    print("=" * 60)


if __name__ == "__main__":
    main()

