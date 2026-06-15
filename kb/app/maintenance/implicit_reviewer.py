"""V1.1: ImplicitRelationReviewer — periodic re-evaluation of low-confidence implicit edges.

Runs on a configurable interval via APScheduler. For each edge with confidence < 0.7:
1. Gathers 3-hop context around both endpoints
2. Re-evaluates the relationship using LLM with richer context
3. Updates confidence if improved, or downgrades if still uncertain
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Neo4jDatabase
from app.llm import LLMClient

logger = logging.getLogger(__name__)

# System prompt for edge re-evaluation (V1.1)
REVIEW_SYSTEM_PROMPT = """你是一个知识图谱审核引擎。重新评估一个现有的隐式关系的置信度。

你会收到：
1. 当前关系的两端节点信息
2. 3-hop 范围内的更广上下文
3. 原始推理证据

请基于更广的上下文，重新判断这个关系是否成立，并给出新的置信度。

## 输出 JSON
{
  "confidence": 0.0-1.0,
  "should_keep": true/false,
  "revised_evidence": "更新后的推理依据（或原始依据如果不变）",
  "reason": "调整理由（一句话）"
}

## 规则
- 如果 3-hop 上下文提供了新的支持证据 → 提高置信度
- 如果 3-hop 上下文显示矛盾或无关 → 降低置信度
- 如果置信度 < 0.3 → should_keep = false
- 只输出 JSON，不要其他内容"""


class ImplicitRelationReviewer:
    """Periodically reviews low-confidence implicit edges and re-evaluates them.

    Uses the same LLM client as the ingest pipeline for consistency.
    """

    # Confidence threshold: edges BELOW this get re-evaluated
    REVIEW_THRESHOLD = 0.7
    # Confidence threshold: edges BELOW this after review get removed
    REMOVAL_THRESHOLD = 0.3
    # Batch size: max edges to review per cycle
    BATCH_SIZE = 10
    # Minimum age before review (seconds): allow edges to "settle"
    MIN_AGE_SECONDS = 3600  # 1 hour

    def __init__(
        self,
        db: Neo4jDatabase,
        llm: LLMClient,
        reasoning_model: str | None = None,
        interval_hours: int = 24,
    ) -> None:
        self._db = db
        self._llm = llm
        self._reasoning_model = reasoning_model
        self._interval_hours = interval_hours
        self._scheduler = AsyncIOScheduler()
        self._job_id = "implicit_review"
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the reviewer scheduler is running."""
        return self._running

    def start(self) -> None:
        """Start the periodic review scheduler."""
        self._scheduler.add_job(
            self._run_review,
            IntervalTrigger(hours=self._interval_hours),
            id=self._job_id,
            replace_existing=True,
            max_instances=1,
        )
        self._scheduler.start()
        self._running = True
        logger.info(
            "ImplicitRelationReviewer started (interval=%dh, threshold=%.1f)",
            self._interval_hours, self.REVIEW_THRESHOLD,
        )

    def shutdown(self) -> None:
        """Shutdown the scheduler."""
        self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("ImplicitRelationReviewer shut down")

    async def _run_review(self) -> None:
        """Main review cycle: fetch, re-evaluate, update edges."""
        try:
            edges = await self._fetch_low_confidence_edges()
            if not edges:
                logger.debug("ImplicitRelationReviewer: no low-confidence edges to review")
                return

            logger.info("ImplicitRelationReviewer: reviewing %d low-confidence edges", len(edges))
            reviewed = 0
            updated = 0
            removed = 0

            for edge in edges:
                try:
                    result = await self._reevaluate_edge(edge)
                    reviewed += 1

                    if result.get("should_keep") is False:
                        await self._remove_edge(edge)
                        removed += 1
                        logger.info("Edge removed: %s→%s (confidence too low after review)",
                                    edge["from_name"], edge["to_name"])
                    elif result.get("confidence", 0) != edge.get("confidence", 0):
                        await self._update_edge(edge, result)
                        updated += 1
                        logger.debug("Edge updated: %s→%s (%.2f → %.2f)",
                                     edge["from_name"], edge["to_name"],
                                     edge.get("confidence", 0), result.get("confidence", 0))
                except Exception as exc:
                    logger.warning("Edge re-evaluation failed for %s→%s: %s",
                                   edge.get("from_name"), edge.get("to_name"), exc)

            logger.info(
                "ImplicitRelationReviewer cycle complete: reviewed=%d updated=%d removed=%d",
                reviewed, updated, removed,
            )

        except Exception:
            logger.exception("ImplicitRelationReviewer cycle failed")

    # ------------------------------------------------------------------
    # Edge fetching
    # ------------------------------------------------------------------

    async def _fetch_low_confidence_edges(self) -> list[dict[str, Any]]:
        """Fetch implicit edges with confidence below threshold, ordered by age."""
        min_age = datetime.now(timezone.utc).timestamp() - self.MIN_AGE_SECONDS
        records = await self._db.execute_read(
            """
            MATCH (a)-[r:IMPLICIT]->(b)
            WHERE r.confidence < $threshold
              AND (r.discovered_at IS NULL
                   OR r.discovered_at < datetime({epochSeconds: $min_age}))
            RETURN a.id AS from_id, a.name AS from_name, a.summary AS from_summary,
                   b.id AS to_id, b.name AS to_name, b.summary AS to_summary,
                   r.type AS rel_type, r.confidence AS confidence,
                   r.evidence AS evidence, r.discovered_at AS discovered_at
            ORDER BY r.confidence ASC
            LIMIT $limit
            """,
            {"threshold": self.REVIEW_THRESHOLD, "min_age": min_age, "limit": self.BATCH_SIZE},
        )
        return [dict(r) for r in records]

    # ------------------------------------------------------------------
    # Edge re-evaluation
    # ------------------------------------------------------------------

    async def _reevaluate_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        """Re-evaluate a single edge using LLM with 3-hop context."""
        from_id = edge["from_id"]
        to_id = edge["to_id"]

        # 1. Gather 3-hop context
        context = await self._gather_3hop_context(from_id, to_id)

        # 2. Build review prompt
        user_prompt = f"""【待审核关系】
源节点: {edge['from_name']}
  {edge.get('from_summary', '无摘要')}

目标节点: {edge['to_name']}
  {edge.get('to_summary', '无摘要')}

关系类型: {edge['rel_type']}
当前置信度: {edge['confidence']}
原始证据: {edge.get('evidence', '无')}

【3-hop 图谱上下文】
{context if context else '无额外上下文'}

请重新评估这个隐式关系并输出 JSON。"""

        try:
            result = await self._llm.chat_json(
                REVIEW_SYSTEM_PROMPT, user_prompt, model=self._reasoning_model,
            )
            if result.get("_parse_error"):
                logger.warning("LLM review failed to produce valid JSON")
                return {"confidence": edge.get("confidence", 0.5), "should_keep": True,
                        "revised_evidence": edge.get("evidence", ""),
                        "reason": "LLM解析失败，保持原置信度"}
            # Clamp confidence
            conf = float(result.get("confidence", edge.get("confidence", 0.5)))
            result["confidence"] = round(max(0.0, min(1.0, conf)), 2)
            return result
        except Exception as exc:
            logger.warning("LLM review call failed: %s", exc)
            return {"confidence": edge.get("confidence", 0.5), "should_keep": True,
                    "revised_evidence": edge.get("evidence", ""),
                    "reason": f"LLM调用失败: {exc}"}

    async def _gather_3hop_context(self, from_id: str, to_id: str) -> str:
        """Gather 3-hop graph context around both endpoints for richer review."""
        try:
            records = await self._db.execute_read(
                """
                MATCH path = (a)-[*1..3]-(m)
                WHERE a.id IN [$from_id, $to_id]
                  AND m.id <> a.id
                RETURN DISTINCT
                    a.name AS source_name,
                    [n in nodes(path) | n.name] AS path_names,
                    [r in relationships(path) | coalesce(r.type, type(r))] AS path_rels,
                    length(path) AS hops
                ORDER BY hops
                LIMIT 30
                """,
                {"from_id": from_id, "to_id": to_id},
            )
            if not records:
                return ""

            lines: list[str] = []
            seen: set[str] = set()
            for r in records:
                names = r.get("path_names", [])
                rels = r.get("path_rels", [])
                key = " → ".join(str(n) for n in names)
                if key not in seen:
                    seen.add(key)
                    path_desc = names[0] if names else "?"
                    for i, rel in enumerate(rels):
                        next_node = names[i + 1] if i + 1 < len(names) else "?"
                        path_desc += f" -[{rel}]-> {next_node}"
                    lines.append(f"  {path_desc}")

            return "\n".join(lines)
        except Exception as exc:
            logger.warning("3-hop context gathering failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Edge updates
    # ------------------------------------------------------------------

    async def _update_edge(self, edge: dict[str, Any], result: dict[str, Any]) -> None:
        """Update confidence and evidence for a reviewed edge."""
        await self._db.execute_write(
            """
            MATCH (a)-[r:IMPLICIT {type: $rel_type}]->(b)
            WHERE a.id = $from_id AND b.id = $to_id
            SET r.confidence = $confidence,
                r.evidence = $evidence,
                r.reviewed_at = datetime(),
                r.review_reason = $reason
            """,
            {
                "from_id": edge["from_id"],
                "to_id": edge["to_id"],
                "rel_type": edge["rel_type"],
                "confidence": result["confidence"],
                "evidence": result.get("revised_evidence", edge.get("evidence", "")),
                "reason": result.get("reason", ""),
            },
        )

    async def _remove_edge(self, edge: dict[str, Any]) -> None:
        """Remove an edge that failed re-evaluation."""
        await self._db.execute_write(
            """
            MATCH (a)-[r:IMPLICIT {type: $rel_type}]->(b)
            WHERE a.id = $from_id AND b.id = $to_id
            DELETE r
            """,
            {
                "from_id": edge["from_id"],
                "to_id": edge["to_id"],
                "rel_type": edge["rel_type"],
            },
        )
