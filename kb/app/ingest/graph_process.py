"""Step 3: Graph processing - compile decisions, node creation, implicit relation discovery."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.database import Neo4jDatabase
from app.llm import LLMClient
from app.models import (
    AnalysisReport,
    CompileAction,
    EntityInfo,
    GraphProcessResult,
    ImplicitRelation,
    ImplicitRelationType,
    MaterialType,
    RelationInfo,
)

logger = logging.getLogger(__name__)

# LLM prompt for node content writing
CONTENT_SYSTEM_PROMPT = """你是一个知识库编译器。请根据提供的信息编写完整的知识页面内容。
输出纯 Markdown 格式，包含：
1. 一句话定义（> 引用格式）
2. 概述（2-5 段）
3. 关键事实（要点列表）
4. 与其他知识的关系（使用 [[实体名]] 格式的 wikilinks）

注意：内容必须完全基于素材，不编造信息。"""

# LLM prompt for implicit relation discovery
IMPLICIT_SYSTEM_PROMPT = """你正在分析一个个人知识库的知识图谱。请发现以下类型的隐式关系：

1. 依赖关系（depends_on）：A 的核心能力依赖 B
2. 对立关系（trade_off）：A 和 B 存在取舍或矛盾
3. 桥接关系（bridges）：A 连接不同知识领域
4. 演化关系（evolves_to）：A 是 B 的前身/替代/演进版本
5. 解决关系（solves）：A 解决了 B 的某个问题

对每个发现的关系，输出 JSON 数组：
[{"source": "实体名", "target": "实体名", "type": "关系类型", "confidence": 0.0-1.0, "evidence": "推理依据"}]

注意：
- 只发现隐式关系，如果两个实体之间已有显式关系则不要重复
- 置信度要保守：只有强有力的推理依据才给 > 0.8
- 只输出 JSON 数组，不要其他内容"""


class GraphProcessor:
    """Core graph processing: compile decisions + node/edge writes + implicit discovery."""

    def __init__(self, llm: LLMClient, db: Neo4jDatabase,
                 compile_model: str | None = None, reasoning_model: str | None = None) -> None:
        self._llm = llm
        self._db = db
        self._compile_model = compile_model
        self._reasoning_model = reasoning_model
        # Prevent GC of background tasks (RES-01 fix)
        self._background_tasks: set[asyncio.Task] = set()

    async def process(
        self,
        analysis: AnalysisReport,
        raw_content: str,
        raw_path: str,
    ) -> GraphProcessResult:
        """Execute the full Step 3: compile → write nodes → write edges → discover implicit."""
        result = GraphProcessResult()

        # 3a. Compile decisions
        actions = self._compile_decision(analysis)

        # 3b + 3c. FAST PATH: simple content (no LLM) + create/update nodes
        node_actions = [a for a in actions
                        if a.action.startswith("create") or a.action.startswith("update")]

        async def _process_fast(action: CompileAction) -> tuple[CompileAction, str]:
            content = self._generate_simple_content(
                action.entity_name, action.label, raw_content, analysis,
            )
            node_id = await self._create_or_update_node(action, content, raw_path)
            return action, node_id

        if node_actions:
            node_results = await asyncio.gather(*[_process_fast(a) for a in node_actions])
            for action, node_id in node_results:
                if action.is_new:
                    result.nodes_created.append(node_id)
                else:
                    result.nodes_updated.append(node_id)
                result.affected_nodes.append(node_id)

        # Deferred: rich LLM content generation in background (fire-and-forget)
        if node_actions:
            task = asyncio.create_task(
                self._enrich_content_background(node_actions, raw_content, analysis, raw_path)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        # 3d. Write explicit relations (batched)
        edge_ids = await self._create_explicit_edges_batch(analysis.relations)
        result.explicit_edges.extend(edge_ids)

        # 3e+3f. Discover and write implicit relations in background (fire-and-forget)
        task = asyncio.create_task(self._discover_and_write_implicit(result.affected_nodes))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # 3g. Update graph structure (batched PageRank)
        await self._update_graph_structure(result.affected_nodes)

        return result

    # ------------------------------------------------------------------
    # 3a. Compile Decision
    # ------------------------------------------------------------------

    def _compile_decision(self, analysis: AnalysisReport) -> list[CompileAction]:
        """Determine Neo4j operations based on analysis report."""
        actions: list[CompileAction] = []

        # Map material type to Neo4j label
        label_map = {
            MaterialType.FACTUAL: "Entity",
            MaterialType.CONCEPTUAL: "Concept",
            MaterialType.EXPERIENTIAL: "Entity",
            MaterialType.COMPARATIVE: "Comparison",
            MaterialType.RELATIONAL: "Entity",
        }
        default_label = label_map.get(analysis.type, "Entity")

        for entity in analysis.entities:
            if entity.exists:
                actions.append(CompileAction(
                    action="update_entity",
                    label="Entity",
                    entity_name=entity.name,
                    is_new=False,
                    properties={"node_id": entity.node_id},
                ))
            else:
                actions.append(CompileAction(
                    action=f"create_{default_label.lower()}",
                    label=default_label,
                    entity_name=entity.name,
                    is_new=True,
                ))

        return actions

    # ------------------------------------------------------------------
    # 3b. Fast Content Generation (no LLM — critical path)
    # ------------------------------------------------------------------

    def _generate_simple_content(
        self,
        entity_name: str,
        label: str,
        raw_content: str,
        analysis: AnalysisReport,
    ) -> str:
        """Generate basic Markdown content without LLM — fast path for critical path."""
        relevant = self._extract_relevant_paragraphs(entity_name, raw_content, max_chars=800)
        related_rels = [
            r for r in analysis.relations
            if r.from_entity == entity_name or r.to_entity == entity_name
        ]

        md = f"> {entity_name}（{label}）\n\n"
        md += f"## 概述\n\n{relevant}\n\n"

        if related_rels:
            md += "## 关系\n\n"
            for r in related_rels:
                target = r.to_entity if r.from_entity == entity_name else r.from_entity
                md += f"- **{r.type}** → [[{target}]]\n"

        md += f"\n---\n*Source: {analysis.compile_suggestion or 'auto-generated'}*\n"
        return md

    @staticmethod
    def _extract_relevant_paragraphs(
        entity_name: str, raw_content: str, max_chars: int = 800,
    ) -> str:
        """Extract paragraphs from raw content that mention the entity."""
        paragraphs = raw_content.split("\n\n")
        relevant: list[str] = []
        total = 0
        # Also check partial name matches for CJK entities
        search_terms = [entity_name]
        if len(entity_name) > 2:
            search_terms.append(entity_name[:len(entity_name) // 2 + 1])
        for p in paragraphs:
            if any(term in p for term in search_terms):
                relevant.append(p.strip())
                total += len(p)
                if total >= max_chars:
                    break
        if not relevant:
            return raw_content[:max_chars]
        return "\n\n".join(relevant)

    # ------------------------------------------------------------------
    # 3b-bg. Background LLM Content Enrichment (fire-and-forget)
    # ------------------------------------------------------------------

    async def _enrich_content_background(
        self,
        node_actions: list[CompileAction],
        raw_content: str,
        analysis: AnalysisReport,
        raw_path: str,
    ) -> None:
        """Background task: enrich node content with LLM and update Neo4j."""
        try:
            async def _enrich_one(action: CompileAction) -> None:
                content = await self._write_node_content(
                    action.entity_name, action.label, raw_content, analysis,
                )
                await self._create_or_update_node(action, content, raw_path)

            await asyncio.gather(*[_enrich_one(a) for a in node_actions])
            logger.info("Background enrichment completed for %d nodes", len(node_actions))
        except Exception as exc:
            logger.warning("Background enrichment failed: %s", exc)

    async def _write_node_content(
        self,
        entity_name: str,
        label: str,
        raw_content: str,
        analysis: AnalysisReport,
    ) -> str:
        """Use LLM to write complete Markdown content for a node."""
        related_summary = await self._get_related_summary(entity_name)

        user_prompt = f"""【原始素材】
{raw_content[:3000]}

【分析报告】
类型: {analysis.type.value}
实体: {', '.join(e.name for e in analysis.entities)}
关系: {', '.join(f'{r.from_entity}→{r.to_entity}({r.type})' for r in analysis.relations)}

【相关已有节点内容摘要】
{related_summary[:1500] if related_summary else '无'}

请为 "{entity_name}" 编写完整的知识页面 Markdown 内容。"""

        try:
            content = await self._llm.chat(CONTENT_SYSTEM_PROMPT, user_prompt, model=self._compile_model)
            return content
        except Exception as exc:
            logger.warning("LLM content writing failed for %s: %s", entity_name, exc)
            return f"> {entity_name}\n\n{raw_content[:2000]}"

    # ------------------------------------------------------------------
    # 3c. Create/Update Nodes
    # ------------------------------------------------------------------

    async def _create_or_update_node(
        self, action: CompileAction, content: str, source_path: str,
    ) -> str:
        """Create or update a node in Neo4j."""
        entity_id = action.entity_name.replace(" ", "_")
        summary = self._extract_summary(content)

        query = f"""
        MERGE (n:{action.label} {{id: $id}})
        SET n.name = $name,
            n.content = $content,
            n.summary = $summary,
            n.source = $source,
            n.updated_at = datetime()
        SET n.created_at = CASE WHEN n.created_at IS NULL
                            THEN datetime() ELSE n.created_at END
        RETURN n.id AS id
        """
        records = await self._db.execute_write(query, {
            "id": entity_id,
            "name": action.entity_name,
            "content": content,
            "summary": summary,
            "source": source_path,
        })
        return records[0]["id"] if records else entity_id

    # ------------------------------------------------------------------
    # 3d. Write Explicit Edges (batched)
    # ------------------------------------------------------------------

    async def _create_explicit_edges_batch(
        self, relations: list[RelationInfo],
    ) -> list[str]:
        """Batch create explicit edges in a single Neo4j transaction."""
        if not relations:
            return []

        edge_data = [
            {
                "from_id": r.from_entity.replace(" ", "_"),
                "to_id": r.to_entity.replace(" ", "_"),
                "edge_type": r.type,
                "context": r.evidence,
            }
            for r in relations
        ]

        query = """
        UNWIND $edges AS edge
        MATCH (a), (b) WHERE a.id = edge.from_id AND b.id = edge.to_id
        MERGE (a)-[r:EXPLICIT {type: edge.edge_type}]->(b)
        SET r.context = edge.context,
            r.created_at = datetime()
        RETURN edge.from_id + '-[' + edge.edge_type + ']->' + edge.to_id AS edge_id
        """
        try:
            records = await self._db.execute_write(query, {"edges": edge_data})
            return [r["edge_id"] for r in records]
        except Exception as exc:
            logger.warning("Batch explicit edge creation failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 3e+3f. Background Implicit Relation Discovery
    # ------------------------------------------------------------------

    async def _discover_and_write_implicit(self, node_ids: list[str]) -> None:
        """Background task: discover and write implicit relations (fire-and-forget)."""
        try:
            implicit_rels = await self._discover_implicit_relations(node_ids)
            for rel in implicit_rels:
                await self._create_implicit_edge(rel)
            if implicit_rels:
                logger.info("Background implicit discovery found %d relations", len(implicit_rels))
        except Exception as exc:
            logger.warning("Background implicit discovery failed: %s", exc)

    # ------------------------------------------------------------------
    # 3e. Implicit Relation Discovery (LLM call) ★
    # ------------------------------------------------------------------

    async def _discover_implicit_relations(
        self, new_node_ids: list[str],
    ) -> list[ImplicitRelation]:
        """Use LLM to discover implicit relations between new and existing nodes."""
        if not new_node_ids:
            return []

        # Build context: new nodes + their neighbors
        context_parts = []
        for node_id in new_node_ids:
            node = await self._db.get_node_by_id(node_id)
            if node:
                context_parts.append(f"【{node.get('name', node_id)}】: {node.get('summary', '无摘要')}")

        # Get neighbors
        neighbor_parts = []
        for node_id in new_node_ids:
            records = await self._db.execute_read(
                """
                MATCH (n)-[r]->(m) WHERE n.id = $id
                RETURN type(r) AS rel_type, CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS semantic,
                       m.name AS target_name, m.summary AS target_summary
                LIMIT 10
                """,
                {"id": node_id},
            )
            for r in records:
                neighbor_parts.append(
                    f"({node_id}) -[:{r.get('semantic', r.get('rel_type', 'RELATED'))}]-> ({r['target_name']})"
                )

        user_prompt = "【新节点】\n" + "\n".join(context_parts) + "\n\n"
        if neighbor_parts:
            user_prompt += "【现有图谱关系】\n" + "\n".join(neighbor_parts) + "\n\n"
        user_prompt += "请发现隐式关系并输出 JSON 数组。如果没有发现隐式关系，输出空数组 []。"

        try:
            result = await self._llm.chat_json(IMPLICIT_SYSTEM_PROMPT, user_prompt, model=self._reasoning_model)
        except Exception as exc:
            logger.warning("Implicit relation discovery failed: %s", exc)
            return []

        if isinstance(result, dict) and result.get("_parse_error"):
            return []

        relations = []
        items = result if isinstance(result, list) else result.get("relations", [])
        if isinstance(items, dict):
            items = items.get("relations", [items])
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                rel_type = ImplicitRelationType(item.get("type", "depends_on"))
            except ValueError:
                rel_type = ImplicitRelationType.DEPENDS_ON
            relations.append(ImplicitRelation(
                source=item.get("source", ""),
                target=item.get("target", ""),
                type=rel_type,
                confidence=float(item.get("confidence", 0.5)),
                evidence=item.get("evidence", ""),
            ))
        return relations

    # ------------------------------------------------------------------
    # 3f. Write Implicit Edges
    # ------------------------------------------------------------------

    async def _create_implicit_edge(self, rel: ImplicitRelation) -> None:
        """Write an IMPLICIT relationship to Neo4j."""
        from_id = rel.source.replace(" ", "_")
        to_id = rel.target.replace(" ", "_")

        query = """
        MATCH (a), (b) WHERE a.id = $from_id AND b.id = $to_id
        MERGE (a)-[r:IMPLICIT {type: $edge_type}]->(b)
        SET r.confidence = $confidence,
            r.evidence = $evidence,
            r.discovered_at = datetime()
        """
        try:
            await self._db.execute_write(query, {
                "from_id": from_id,
                "to_id": to_id,
                "edge_type": rel.type.value,
                "confidence": rel.confidence,
                "evidence": rel.evidence,
            })
        except Exception as exc:
            logger.warning("Failed to create implicit edge %s→%s: %s", from_id, to_id, exc)

    # ------------------------------------------------------------------
    # 3g. Graph Structure Update (batched)
    # ------------------------------------------------------------------

    async def _update_graph_structure(self, affected_nodes: list[str]) -> None:
        """Batch update graph structure using UNWIND.

        NOTE (CQ-01): This uses a simplified in-degree heuristic
        (page_rank = in_degree / 10.0) rather than true GDS PageRank.
        Suitable for small personal knowledge graphs (<1000 nodes).
        Upgrade to GDS PageRank when graph size warrants it.
        """
        if not affected_nodes:
            return
        query = """
        UNWIND $node_ids AS node_id
        MATCH (n) WHERE n.id = node_id
        OPTIONAL MATCH (other)-[r]->(n)
        WITH n, count(r) AS in_degree
        SET n.page_rank = CASE WHEN in_degree > 0
                          THEN toFloat(in_degree) / 10.0
                          ELSE 0.01 END
        """
        try:
            await self._db.execute_write(query, {"node_ids": affected_nodes})
        except Exception as exc:
            logger.warning("Batch PageRank update failed: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_related_summary(self, entity_name: str) -> str:
        """Get summary of related nodes for LLM context."""
        records = await self._db.execute_read(
            "MATCH (n) WHERE n.name CONTAINS $name OR n.id CONTAINS $name "
            "RETURN n.name AS name, n.summary AS summary LIMIT 5",
            {"name": entity_name},
        )
        return "\n".join(f"- {r['name']}: {r.get('summary', '')}" for r in records)

    @staticmethod
    def _extract_summary(content: str) -> str:
        """Extract a one-line summary from Markdown content."""
        # Look for blockquote first line
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith(">"):
                return line.lstrip("> ").strip()
        # Fall back to first non-empty, non-heading line
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and len(line) > 10:
                return line[:100]
        return content[:100]
