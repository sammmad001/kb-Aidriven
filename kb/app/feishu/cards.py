"""Feishu card builders: 12 card types for structured message display."""

from __future__ import annotations


from app.models import AnalysisReport, GraphStats, IngestResult, QueryResult, QueryType


def _card(template_id: str, header_title: str, elements: list[dict],
          header_color: str = "blue") -> dict:
    """Base card structure builder."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": header_color,
        },
        "elements": elements,
    }


def _md(text: str) -> dict:
    """Markdown element."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _hr() -> dict:
    """Horizontal rule."""
    return {"tag": "hr"}


def _action(buttons: list[dict]) -> dict:
    """Action button group."""
    return {"tag": "action", "actions": buttons}


def _button(text: str, value: dict | None = None, btn_type: str = "primary") -> dict:
    """Single button."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "value": value or {},
    }


# ======================================================================
# Input Feedback Cards
# ======================================================================

def build_ack_card(task_id: str, msg_type: str) -> dict:
    """Level 1: Instant acknowledgment card (< 1s)."""
    return _card(
        "ack",
        "📋 已收到",
        [
            _md(f"**消息类型**: {msg_type}\n**任务ID**: `{task_id}`\n\n正在处理中，请稍候..."),
        ],
        header_color="blue",
    )


def build_analysis_card(analysis: AnalysisReport) -> dict:
    """Level 2: Classification feedback card (~3s)."""
    type_emoji = {
        "factual": "📌", "conceptual": "💡", "experiential": "🔧",
        "comparative": "⚖️", "relational": "🔗",
    }
    emoji = type_emoji.get(analysis.type.value, "📄")
    entity_names = ", ".join(e.name for e in analysis.entities) or "无"
    conflict_count = len(analysis.conflicts)

    elements = [
        _md(f"{emoji} **分类结果**: {analysis.type.value}"),
        _md(f"**识别实体**: {entity_names}"),
    ]
    if conflict_count:
        elements.append(_md(f"⚠️ **检测到矛盾**: {conflict_count} 处"))
    if analysis.gaps:
        elements.append(_md(f"🔍 **知识缺口**: {'; '.join(analysis.gaps[:3])}"))
    elements.append(_hr())
    elements.append(_md("_正在编译知识图谱..._"))

    return _card("analysis", "📊 分析完成", elements, header_color="turquoise")


def build_complete_card(result: IngestResult) -> dict:
    """Complete notification card: show new and updated knowledge points."""
    gr = result.graph_result
    analysis = result.analysis

    # Extract new knowledge points from graph_result (ground truth)
    new_nodes: list[str] = []
    updated_nodes: list[str] = []
    if gr:
        # graph_result.nodes_created contains actual node IDs that were created
        new_nodes = [n.replace("_", " ") for n in gr.nodes_created]
        updated_nodes = [n.replace("_", " ") for n in gr.nodes_updated]

    # Fallback: use analysis.entities if graph_result is empty
    if not new_nodes and not updated_nodes and analysis and analysis.entities:
        new_nodes = [e.name for e in analysis.entities if not e.exists]
        updated_nodes = [e.name for e in analysis.entities if e.exists]

    # Extract relations
    relations: list[str] = []
    if analysis and analysis.relations:
        for rel in analysis.relations[:5]:
            relations.append(f"{rel.from_entity} → {rel.to_entity} ({rel.type})")

    # Build elements
    elements = [
        _md("知识已完成推理和分类："),
        _hr(),
    ]

    # New knowledge points section
    if new_nodes:
        items = "\n".join(f"- {name}" for name in new_nodes[:8])
        elements.append(_md(f"📌 **新增知识点**\n{items}"))

    # Updated knowledge points section
    if updated_nodes:
        items = "\n".join(f"- {name}" for name in updated_nodes[:8])
        elements.append(_md(f"🔄 **更新知识点**\n{items}"))

    # If neither new nor updated, show summary
    if not new_nodes and not updated_nodes:
        elements.append(_md("📌 **新增知识点**: 0 个"))

    # Related knowledge points section
    if relations:
        rel_items = "\n".join(f"- {r}" for r in relations)
        elements.append(_md(f"🔗 **关联知识点**\n{rel_items}"))
    elif gr and gr.explicit_edges:
        elements.append(_md(f"🔗 **关联知识点**: {len(gr.explicit_edges)} 条关系"))

    # Stats footer
    elements.append(_hr())
    n_new = len(gr.nodes_created) if gr else 0
    n_upd = len(gr.nodes_updated) if gr else 0
    n_edges = len(gr.explicit_edges) if gr else 0
    elements.append(_md(f"统计: 新增 **{n_new}** 节点 | 更新 **{n_upd}** 节点 | 关联 **{n_edges}** 条"))

    return _card("complete", "🎉 知识收录完成", elements, header_color="green")


# ======================================================================
# Query Result Cards
# ======================================================================

def build_factual_result_card(query_result: QueryResult) -> dict:
    """Fact query result: single node content display."""
    answer = query_result.answer[:1500]
    sources = "\n".join(f"  - {s.node_name}" for s in query_result.sources)

    elements = [
        _md(answer),
        _hr(),
        _md(f"**来源**: \n{sources}" if sources else "_无引用来源_"),
    ]
    return _card("factual", "📌 知识查询", elements, header_color="blue")


def build_relational_result_card(query_result: QueryResult) -> dict:
    """Relational query result: show relationships."""
    answer = query_result.answer[:1500]

    elements = [_md(answer)]

    if query_result.implicit_relations_used:
        elements.append(_hr())
        elements.append(_md("**推理线索**:"))
        for rel in query_result.implicit_relations_used[:5]:
            elements.append(_md(
                f"- *{rel.type.value}* {rel.source} → {rel.target} ({rel.confidence:.0%})"
            ))

    elements.append(_hr())
    elements.append(_md(f"置信度: **{query_result.confidence:.0%}**"))

    return _card("relational", "🔗 关联分析", elements, header_color="purple")


def build_reasoning_result_card(query_result: QueryResult) -> dict:
    """Reasoning query result: multi-hop paths + implicit relations."""
    answer = query_result.answer[:2000]

    elements = [
        _md(answer),
    ]

    if query_result.sources:
        elements.append(_hr())
        elements.append(_md("**引用来源**:"))
        for src in query_result.sources[:8]:
            elements.append(_md(f"  - {src.node_name}"))

    if query_result.implicit_relations_used:
        elements.append(_hr())
        elements.append(_md("**推理依据** (系统发现的隐式关系):"))
        for rel in query_result.implicit_relations_used[:8]:
            elements.append(_md(
                f"  {rel.source} --[{rel.type.value}]--> {rel.target} "
                f"(置信度 {rel.confidence:.0%})"
            ))

    elements.append(_hr())
    elements.append(_md(f"推理置信度: **{query_result.confidence:.0%}**"))

    return _card("reasoning", "🧠 推理分析", elements, header_color="indigo")


def build_global_result_card(query_result: QueryResult) -> dict:
    """Global query result: overview with cluster info."""
    answer = query_result.answer[:2000]

    elements = [_md(answer), _hr()]

    if query_result.sources:
        elements.append(_md(f"**涉及 {len(query_result.sources)} 个知识节点**"))

    elements.append(_md(f"综合置信度: **{query_result.confidence:.0%}**"))

    return _card("global", "🌐 全局分析", elements, header_color="violet")


# ======================================================================
# Utility Cards
# ======================================================================

def build_stats_card(stats: GraphStats) -> dict:
    """Knowledge graph statistics card."""
    return _card(
        "stats", "📊 知识库统计",
        [
            _md(
                f"- **知识节点**: {stats.node_count}\n"
                f"- **关系边数**: {stats.edge_count}\n"
                f"- **隐式关系**: {stats.implicit_edge_count}\n"
                f"- **知识群落**: {stats.cluster_count}\n"
                f"- **实体数**: {stats.entity_count}\n"
                f"- **概念数**: {stats.concept_count}"
            ),
        ],
        header_color="blue",
    )


def build_error_card(error_type: str, message: str, suggestion: str = "") -> dict:
    """Error notification card."""
    elements = [
        _md(f"❌ **{error_type}**\n\n{message}"),
    ]
    if suggestion:
        elements.append(_hr())
        elements.append(_md(f"💡 _{suggestion}_"))

    return _card("error", "出错了", elements, header_color="red")


def build_help_card() -> dict:
    """Help / command reference card."""
    return _card(
        "help", "📖 使用帮助",
        [
            _md(
                "**知识输入** (直接发送消息):\n"
                "- 发送文本、图片、文件、链接\n"
                "- 系统自动分析、分类、存入知识图谱\n"
                "- 疑问句自动识别为查询，无需手动加 /q\n\n"
                "**知识查询**:\n"
                "- `/q RAG是什么` — 事实查询\n"
                "- `/q RAG和知识图谱的关系` — 关联查询\n"
                "- `/q 为什么选择Graph-First架构` — 推理查询\n"
                "- 或直接发送问题（系统自动识别）\n\n"
                "**深度研究**:\n"
                "- `/research 量子计算最新进展` — 触发 MiroMind 深度研究并自动入库\n\n"
                "**账户注册与绑定**:\n"
                "- `/register 用户名 密码` — 直接注册新账户并绑定（无需 Web 端）\n"
                "- `/bind 用户名 密码` — 绑定已有 Web 账户\n"
                "- `/unbind` — 解除绑定\n"
                "- `/whoami` — 查看当前绑定状态\n\n"
                "**其他命令**:\n"
                "- `/stats` — 知识库统计\n"
                "- `/search 关键词` — 搜索节点\n"
                "- `/recent [数量] [类型] [时间]` — 最近记录\n"
                "- `/end` — 清空对话上下文\n"
                "- `/help` — 显示此帮助"
            ),
        ],
        header_color="green",
    )


# Query type to card builder mapping
QUERY_CARD_BUILDERS = {
    QueryType.FACTUAL: build_factual_result_card,
    QueryType.RELATIONAL: build_relational_result_card,
    QueryType.REASONING: build_reasoning_result_card,
    QueryType.GLOBAL: build_global_result_card,
}


def build_query_card(query_result: QueryResult) -> dict:
    """Build the appropriate card based on query type, with follow-up hint."""
    builder = QUERY_CARD_BUILDERS.get(query_result.query_type, build_factual_result_card)
    card = builder(query_result)
    # Append follow-up hint to encourage natural conversation
    card["elements"].append(_hr())
    card["elements"].append(_md("💡 _可直接追问，如「它的缺点是什么？」_"))
    return card


def build_recent_card(records: list[dict]) -> dict:
    """Build an enhanced /recent card with grouping, relative time, and type labels.

    Args:
        records: List of dicts with keys: name, summary, updated, labels (optional).
    """
    if not records:
        return _card(
            "recent", "📝 最近更新",
            [_md("知识库暂无内容")],
            header_color="blue",
        )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    items: list[str] = []
    for r in records:
        name = r.get("name", "?")
        summary = r.get("summary", "")
        labels = r.get("labels", [])
        updated = r.get("updated", "")

        # Relative time
        time_str = ""
        if updated:
            try:
                # Parse Neo4j datetime string or ISO format
                if hasattr(updated, "isoformat"):
                    dt = updated
                elif isinstance(updated, str):
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                else:
                    dt = None
                if dt:
                    diff = now - dt if dt.tzinfo else now.replace(tzinfo=None) - dt
                    days = diff.days
                    if days == 0:
                        time_str = "今天"
                    elif days == 1:
                        time_str = "昨天"
                    elif days < 7:
                        time_str = f"{days}天前"
                    elif days < 30:
                        time_str = f"{days // 7}周前"
                    else:
                        time_str = f"{days // 30}月前"
            except Exception:
                time_str = ""

        # Type label
        type_tag = ""
        if labels:
            if "Entity" in labels:
                type_tag = "📌 "
            elif "Concept" in labels:
                type_tag = "💡 "
            elif "Comparison" in labels:
                type_tag = "⚖️ "

        # Truncate summary
        if summary:
            summary = summary[:80] + ("..." if len(summary) > 80 else "")
            items.append(f"- {type_tag}**{name}** ({time_str})\n  {summary}")
        else:
            items.append(f"- {type_tag}**{name}** ({time_str})")

    elements = [
        _md("\n".join(items)),
        _hr(),
        _md(f"共 **{len(records)}** 条记录"),
    ]

    return _card("recent", "📝 最近更新", elements, header_color="blue")


# ======================================================================
# Account Binding Cards
# ======================================================================

def build_unbound_prompt_card() -> dict:
    """Card shown to unbound Feishu users — guides them to register + bind."""
    return _card(
        "unbound_prompt", "🔒 尚未绑定账户",
        [
            _md(
                "你还没有绑定知识库账户。\n\n"
                "**方式一：飞书直接注册（推荐）**\n"
                "```\n/register 用户名 密码\n```\n\n"
                "一步完成注册 + 绑定，无需打开 Web 端。\n\n"
                "**方式二：绑定已有 Web 账户**\n"
                "```\n/bind 用户名 密码\n```\n\n"
                "如果你已在 Web 端注册过，使用此命令绑定。\n\n"
                "💡 绑定后，飞书和 Web 端的知识将完全互通。"
            ),
            _hr(),
            _md("输入 `/help` 查看完整命令列表"),
        ],
        header_color="orange",
    )


def build_register_success_card(username: str, migrated_nodes: int = 0) -> dict:
    """Card shown after successful registration + auto-binding from Feishu."""
    migration_note = ""
    if migrated_nodes > 0:
        migration_note = f"\n\n📦 已自动迁移 **{migrated_nodes}** 个知识节点"

    return _card(
        "register_success", "✅ 注册成功",
        [
            _md(
                f"账户 **{username}** 已创建并自动绑定。{migration_note}\n\n"
                "现在你可以直接发送消息，所有操作都在此账户下进行。\n"
                "Web 端和飞书端的知识完全互通。\n\n"
                "💡 建议妥善保管你的用户名和密码。"
            ),
        ],
        header_color="green",
    )


def build_register_error_card(error_msg: str) -> dict:
    """Card shown when registration fails.

    error_msg values: already_bound, username_exists, invalid_format, invalid_password
    """
    hints = {
        "already_bound": "当前飞书已绑定账户，如需重新注册请先 `/unbind`",
        "username_exists": "用户名已被占用，换一个试试，或使用 `/bind` 绑定已有账户",
        "invalid_format": "用户名需 3-50 个字符，仅支持字母、数字、下划线和连字符",
        "invalid_password": "密码至少 6 个字符",
    }
    hint = hints.get(error_msg, "请检查输入格式后重试")

    return _card(
        "register_error", "❌ 注册失败",
        [
            _md(f"**{hint}**"),
            _hr(),
            _md(
                "**正确格式**：`/register 用户名 密码`\n\n"
                "要求：\n"
                "- 用户名：3-50 字符（字母、数字、`_`、`-`）\n"
                "- 密码：至少 6 字符，不含空格\n\n"
                "如已有 Web 账户，请使用 `/bind 用户名 密码`"
            ),
        ],
        header_color="red",
    )


def build_bind_success_card(username: str, migrated_nodes: int = 0) -> dict:
    """Card shown after successful binding."""
    migration_note = ""
    if migrated_nodes > 0:
        migration_note = f"\n\n📦 已自动迁移 **{migrated_nodes}** 个知识节点"

    return _card(
        "bind_success", "✅ 绑定成功",
        [
            _md(
                f"飞书账户已绑定到 **{username}**。{migration_note}\n\n"
                "现在你可以直接发送消息，所有操作都在同一账户下进行。\n"
                "Web 端和飞书端的知识完全互通。"
            ),
        ],
        header_color="green",
    )


def build_bind_error_card(error_msg: str) -> dict:
    """Card shown when binding fails."""
    return _card(
        "bind_error", "❌ 绑定失败",
        [
            _md(f"**{error_msg}**"),
            _hr(),
            _md(
                "**正确格式**：`/bind 用户名 密码`\n\n"
                "请确保：\n"
                "- 用户名和密码正确\n"
                "- 已在 Web 端注册账户\n"
                "- 密码中不含空格"
            ),
        ],
        header_color="red",
    )


def build_whoami_card(binding: dict) -> dict:
    """Card showing current binding status."""
    if binding.get("bound"):
        username = binding.get("username", "未知")
        user_id = binding.get("user_id", "")
        elements = [
            _md(
                f"**当前状态**：已绑定\n"
                f"**用户名**：{username}\n"
                f"**用户ID**：`{user_id}`"
            ),
            _hr(),
            _md("如需解绑，发送 `/unbind`"),
        ]
    else:
        elements = [
            _md(
                "**当前状态**：未绑定\n\n"
                "**方式一**：直接在飞书注册\n"
                "`/register 用户名 密码`\n\n"
                "**方式二**：绑定已有 Web 账户\n"
                "`/bind 用户名 密码`"
            ),
        ]

    return _card("whoami", "👤 账户信息", elements, header_color="blue")


def build_unbind_card() -> dict:
    """Card shown after successful unbind."""
    return _card(
        "unbind", "🔓 已解绑",
        [
            _md(
                "飞书账户已解除绑定。\n\n"
                "如需重新绑定，发送 `/bind 用户名 密码`"
            ),
        ],
        header_color="grey",
    )
