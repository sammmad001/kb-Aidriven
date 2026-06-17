"""Step 3: LLM answer generation based on graph retrieval results."""

from __future__ import annotations

import logging
from typing import Any

from app.llm import LLMClient
from app.models import (
    ImplicitRelation,
    ImplicitRelationType,
    QueryResult,
    QueryType,
    RetrievalResult,
    SourceReference,
)

logger = logging.getLogger(__name__)

ANSWER_SYSTEM_PROMPT = """你是一个个人知识库的智能助手。请基于从知识图谱中检索到的信息回答用户的问题。

要求：
1. 基于检索到的信息回答，不编造知识库中没有的内容
2. 引用来源：在关键论断后标注 [来源: 节点名]
3. 区分确定事实和推理结论
4. 对于隐式关系线索，说明这是系统推理发现的关系，标注置信度
5. 如果检索结果不足以回答问题，坦诚说明
6. 如果提供了对话历史，结合上下文理解用户追问的意图"""


class AnswerGenerator:
    """Generate answers using LLM based on graph retrieval results."""

    def __init__(self, llm: LLMClient, model: str | None = None) -> None:
        self._llm = llm
        self._model = model

    async def generate(
        self,
        question: str,
        retrieval: RetrievalResult,
        query_type: QueryType = QueryType.FACTUAL,
        context_history: list[dict[str, Any]] | None = None,
    ) -> QueryResult:
        """Generate a structured answer based on retrieval results."""
        # Special case: factual with single node + direct content → no LLM needed
        if query_type == QueryType.FACTUAL and retrieval.nodes:
            if len(retrieval.nodes) == 1:
                node = retrieval.nodes[0]
                content = node.get("content", "")
                if content:
                    return QueryResult(
                        answer=content,
                        sources=[SourceReference(
                            node_id=node.get("id", ""),
                            node_name=node.get("name", ""),
                            relevance=1.0,
                        )],
                        query_type=query_type,
                        depth=0,
                        confidence=1.0,
                    )
            # Multi-node factual: fall through to LLM synthesis for comprehensive answer

        # Build user prompt with retrieval context
        user_prompt = self._build_prompt(question, retrieval, context_history)

        # Call LLM
        try:
            answer_text = await self._llm.chat(ANSWER_SYSTEM_PROMPT, user_prompt, model=self._model)
        except Exception as exc:
            logger.error("LLM answer generation failed: %s", exc)
            # Fallback: return raw retrieval data
            return self._fallback_answer(question, retrieval, query_type)

        # Build result
        sources = [
            SourceReference(
                node_id=n.get("id", ""),
                node_name=n.get("name", ""),
                relevance=0.9,
            )
            for n in retrieval.nodes if n.get("id")
        ]

        implicit_used = []
        for ir in retrieval.implicit_relations:
            try:
                rel_type = ImplicitRelationType(ir.get("rel_type", "depends_on"))
            except ValueError:
                rel_type = ImplicitRelationType.DEPENDS_ON
            implicit_used.append(ImplicitRelation(
                source=ir.get("from_name", ""),
                target=ir.get("to_name", ""),
                type=rel_type,
                confidence=float(ir.get("confidence", 0.5)),
                evidence=ir.get("evidence", ""),
            ))

        return QueryResult(
            answer=answer_text,
            sources=sources,
            implicit_relations_used=implicit_used,
            confidence=self._estimate_confidence(retrieval),
            query_type=query_type,
            depth=len(retrieval.explicit_paths) + len(retrieval.implicit_relations),
        )

    def _build_prompt(
        self,
        question: str,
        retrieval: RetrievalResult,
        context_history: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build the user prompt with retrieval context."""
        parts = [f"【用户问题】\n{question}\n"]

        # Include conversation history for follow-up context
        if context_history:
            history_lines: list[str] = []
            for msg in context_history[-6:]:  # last 3 turns (6 messages)
                role = msg.get("role", "")
                content = msg.get("content", "")[:300]
                if role == "user":
                    history_lines.append(f"  用户: {content}")
                elif role == "assistant":
                    history_lines.append(f"  助手: {content}")
            if history_lines:
                parts.append("【对话历史】")
                parts.extend(history_lines)
                parts.append("")

        if retrieval.nodes:
            parts.append("【检索到的知识节点】")
            for node in retrieval.nodes:
                name = node.get("name", "Unknown")
                content = node.get("content", node.get("summary", ""))
                if content:
                    # Truncate very long content
                    if len(content) > 500:
                        content = content[:500] + "..."
                    parts.append(f"- {name}: {content}")

        if retrieval.explicit_paths:
            parts.append("\n【显式关系路径】")
            for path in retrieval.explicit_paths:
                from_name = path.get("from_name", "?")
                rel_type = path.get("rel_type", "?")
                to_name = path.get("to_name", "?")
                parts.append(f"  {from_name} → [{rel_type}] → {to_name}")

        if retrieval.implicit_relations:
            parts.append("\n【隐式关系线索】")
            for ir in retrieval.implicit_relations:
                from_name = ir.get("from_name", "?")
                rel_type = ir.get("rel_type", "?")
                to_name = ir.get("to_name", "?")
                confidence = ir.get("confidence", 0)
                evidence = ir.get("evidence", "")
                parts.append(
                    f"  {from_name} --[{rel_type}, 置信度 {confidence:.0%}]--> {to_name}"
                    + (f"\n  推理依据：{evidence}" if evidence else "")
                )

        if retrieval.bridge_entities:
            parts.append("\n【桥接实体（跨界枢纽）】")
            for bridge in retrieval.bridge_entities:
                parts.append(f"  - {bridge.get('name', '?')}: {bridge.get('summary', '')}")

        if retrieval.cluster_info:
            parts.append("\n【知识群落信息】")
            for cluster in retrieval.cluster_info:
                parts.append(f"  - {cluster.get('label', '?')}: {cluster.get('summary', '')}")

        return "\n".join(parts)

    def _fallback_answer(
        self, question: str, retrieval: RetrievalResult, query_type: QueryType,
    ) -> QueryResult:
        """Generate a fallback answer when LLM fails."""
        parts = ["抱歉，LLM 生成回答时出错。以下是检索到的相关内容：\n"]
        for node in retrieval.nodes:
            name = node.get("name", "")
            summary = node.get("summary", "")
            if summary:
                parts.append(f"- **{name}**: {summary}")

        return QueryResult(
            answer="\n".join(parts),
            sources=[SourceReference(node_id=n.get("id", ""), node_name=n.get("name", ""))
                     for n in retrieval.nodes if n.get("id")],
            query_type=query_type,
            confidence=0.3,
        )

    @staticmethod
    def _estimate_confidence(retrieval: RetrievalResult) -> float:
        """Estimate answer confidence based on retrieval quality."""
        if not retrieval.nodes:
            return 0.1
        score = 0.5
        if retrieval.explicit_paths:
            score += 0.2
        if retrieval.implicit_relations:
            avg_conf = sum(
                ir.get("confidence", 0.5) for ir in retrieval.implicit_relations
            ) / max(len(retrieval.implicit_relations), 1)
            score += avg_conf * 0.2
        if len(retrieval.nodes) >= 2:
            score += 0.1
        return min(round(score, 2), 1.0)
