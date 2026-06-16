"""Feishu card builders for MiroMind research results."""

from __future__ import annotations

from app.feishu.cards import _card, _md, _hr
from app.services.miromind_client import ResearchResult


def build_research_result_card(
    question: str,
    result: ResearchResult,
    ingest_summary: str = "",
) -> dict:
    """Build a card displaying MiroMind research results.

    Args:
        question: The original research question.
        result: The ResearchResult from MiroMindClient.
        ingest_summary: Optional summary of KB ingestion (e.g. "已入库 3 个知识点").
    """
    # Truncate content for card display (≤2000 chars per LENGTH_CONSTRAINT)
    content = result.content[:3000] if result.content else "（无内容）"

    elements: list[dict] = [
        _md(f"**🔍 研究问题**\n{question}"),
        _hr(),
    ]

    if result.status == "error" or not result.content:
        elements.append(_md(f"❌ 研究失败: {result.error or '未知错误'}"))
        return _card("research_error", "🔬 深度研究", elements, header_color="red")

    elements.append(_md(content))

    # Metadata footer
    meta_parts: list[str] = []
    if result.total_tokens:
        meta_parts.append(f"Token: **{result.total_tokens}**")
    if result.duration_ms:
        meta_parts.append(f"耗时: **{result.duration_ms / 1000:.1f}s**")
    if result.model:
        meta_parts.append(f"模型: `{result.model}`")

    if meta_parts:
        elements.append(_hr())
        elements.append(_md(" | ".join(meta_parts)))

    # Ingestion status
    if ingest_summary:
        elements.append(_hr())
        elements.append(_md(f"📚 **知识入库**: {ingest_summary}"))

    return _card(
        "research_result",
        "🔬 MiroMind 深度研究",
        elements,
        header_color="indigo",
    )


def build_research_unavailable_card() -> dict:
    """Build a card shown when MiroMind is not configured or unavailable."""
    return _card(
        "research_unavailable",
        "🔬 MiroMind 不可用",
        [
            _md(
                "MiroMind 深度研究功能未启用。\n\n"
                "可能原因：\n"
                "- `MIROMIND_API_KEY` 未配置\n"
                "- MiroMind API 服务不可达\n\n"
                "请联系管理员配置后重试。"
            ),
        ],
        header_color="orange",
    )
