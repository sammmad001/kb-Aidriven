"""Step 3: Graph processing - compile decisions, node creation, implicit relation discovery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.database import Neo4jDatabase
from app.ingest.fewshot import load_fewshot
from app.llm import LLMClient
from app.models import (
    AnalysisReport,
    CompileAction,
    GraphProcessResult,
    ImplicitRelation,
    ImplicitRelationType,
    MaterialType,
    RelationInfo,
)

logger = logging.getLogger(__name__)

# LLM prompt for node content writing (V1.1: enriched with few-shot guidance)
CONTENT_SYSTEM_PROMPT = """你是一个知识库编译器。请根据提供的信息编写完整的知识页面内容。

输出纯 Markdown 格式，包含：
1. 一句话定义（> 引用格式）
2. 概述（2-5 段，基于素材内容展开，不编造信息）
3. 关键事实（要点列表，每项附带来源引用）
4. 与其他知识的关系（使用 [[实体名]] 格式的 wikilinks）
5. 参考来源（如果有）

编写原则：
- 内容必须完全基于素材，不编造信息
- 关键数据点保留原始出处
- wikilinks 使用规范名称（如 [[Kubernetes]] 而非 [[K8s]]）
- 与其他知识的关联段落应引用分析报告中的关系信息"""

# LLM prompt for implicit relation discovery (V1.1: reasoning strategy + confidence calibration + anti-patterns)
IMPLICIT_SYSTEM_PROMPT = """你是一个知识图谱推理引擎。基于给定的新节点及其图谱上下文，发现隐含的语义关系。

## 推理策略（按顺序执行）

### 第一步：排除已有关系
检查新节点与上下文中每个实体的关系是否已被显式边（EXPLICIT）记录。
如果已存在 EXPLCIT 边 → 跳过，不重复推理。

### 第二步：按类型逐项推理

#### 1. depends_on（依赖关系）
**触发条件**：A 的核心功能/能力/存在前提依赖 B
**反例（不是依赖）**：A "提到"B、"与B有关"、"使用B的API"（这是 uses，不是依赖）
**置信度指南**：
- 0.9+：A 在定义中明确依赖 B（如"Kubernetes 依赖 etcd 存储集群状态"）
- 0.7-0.9：A 的核心流程中 B 是必要组件
- 0.5-0.7：A 的某些场景需要 B，但非核心

#### 2. trade_off（对立/取舍关系）
**触发条件**：A 和 B 在同一目标维度上存在此消彼长的关系
**要求**：必须在 evidence 中指明对立的维度（如"性能 vs 成本"、"灵活性 vs 复杂度"）

#### 3. bridges（桥接关系）
**触发条件**：A 连接了两个明显不同的知识领域/群落
**要求**：必须在 evidence 中指明被桥接的两个领域

#### 4. evolves_to（演化关系）
**触发条件**：A 是 B 的前身/替代/下一代版本
**关键证据**：时序先后 + 功能替代/增强关系
**注意**：仅凭"A在B之后出现"不足以判定，需要功能演化证据

#### 5. solves（解决关系）
**触发条件**：A 解决了 B 中明确指出的问题/痛点
**要求**：必须在 evidence 中指明被解决的具体问题

#### 6. precedes（时序先后关系）🆕 V1.1
**触发条件**：A 在时间/逻辑顺序上先于 B，且这种先后关系具有知识意义
**反例（不是 precedes）**：A 和 B 只是同时出现、A 的文档发布时间早于 B 但无因果关系
**置信度指南**：
- 0.9+：明确的先后顺序 + 因果关联（如"TCP 握手 precedes 数据传输"）
- 0.7-0.9：明确的时序先后，但无直接因果
- 0.5-0.7：合理推断的先后关系

#### 7. causes（因果关系）🆕 V1.1
**触发条件**：A 是导致 B 发生/存在的直接原因
**反例（不是 causes）**：A 和 B 正相关但无因果证据（相关≠因果）
**要求**：必须能在 evidence 中描述因果链条
**置信度指南**：
- 0.9+：有明确的因果关系陈述（"A 导致 B"、"因为 A 所以 B"）
- 0.7-0.9：强因果推理链条
- 0.5-0.7：合理但非直接因果

#### 8. contradicts（矛盾关系）🆕 V1.1
**触发条件**：A 和 B 在同一事实上给出矛盾/对立的观点或结论
**反例（不是 contradicts）**：A 和 B 讨论不同层面/角度，不是正面冲突
**要求**：必须在 evidence 中指明冲突的具体维度
**置信度指南**：
- 0.9+：同一明确事实的正面矛盾陈述
- 0.7-0.9：立场鲜明对立，但非同一事实
- 0.5-0.7：观点分歧，可能调和

#### 9. analogous_to（类比关系）🆕 V1.1
**触发条件**：A 和 B 在结构/功能/原理上具有可比性，可以从一个理解另一个
**反例（不是 analogous_to）**：A 和 B 只是同一类别（如"都是数据库"→这不是类比）
**要求**：必须在 evidence 中指明类比的具体维度
**置信度指南**：
- 0.9+：明确的结构同构/功能对应（如"知识图谱中的节点类比数据库中的行"）
- 0.7-0.9：多维度可类比
- 0.5-0.7：单维度类比

## 置信度校准指南
- 0.9-1.0：有直接文本证据支持，几乎确定
- 0.7-0.9：强推理链，多条间接证据
- 0.5-0.7：合理推理，存在一定不确定性
- 0.3-0.5：猜测性推理，证据不足
- < 0.3：不应输出（质量不可接受）

## 输出 JSON 数组
[{"source": "实体名", "target": "实体名", "type": "depends_on|trade_off|bridges|evolves_to|solves|precedes|causes|contradicts|analogous_to", "confidence": 0.0-1.0, "evidence": "推理依据（引用具体信息源）"}]

## 规则
- 如果未发现任何隐式关系，输出空数组 []
- 每个 source-target 对最多输出一个隐式关系
- 推理依据必须引用新节点摘要中的具体信息
- 不要推理"相关"、"类似"等模糊关系
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
            props: dict[str, Any] = {}
            if entity.exists and entity.node_id:
                props["node_id"] = entity.node_id
            # V1.1: Pass entity metadata for node enrichment
            if entity.aliases:
                props["aliases"] = entity.aliases
            if entity.subtype:
                props["subtype"] = entity.subtype
            if entity.domain:
                props["domain"] = entity.domain
            if entity.definition:
                props["definition"] = entity.definition
            if entity.importance:
                props["importance"] = entity.importance

            if entity.exists:
                actions.append(CompileAction(
                    action="update_entity",
                    label="Entity",
                    entity_name=entity.name,
                    is_new=False,
                    properties=props,
                ))
            else:
                actions.append(CompileAction(
                    action=f"create_{default_label.lower()}",
                    label=default_label,
                    entity_name=entity.name,
                    is_new=True,
                    properties=props,
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
        props = action.properties

        query = f"""
        MERGE (n:{action.label} {{id: $id, user_id: $_user_id}})
        SET n.name = $name,
            n.content = $content,
            n.summary = $summary,
            n.source = $source,
            n.user_id = $_user_id,
            n.updated_at = datetime()
        SET n.created_at = CASE WHEN n.created_at IS NULL
                            THEN datetime() ELSE n.created_at END
        """
        params: dict[str, Any] = {
            "id": entity_id,
            "name": action.entity_name,
            "content": content,
            "summary": summary,
            "source": source_path,
        }

        # V1.1: Store entity enrichment fields
        if props.get("aliases"):
            query += "\n        SET n.aliases = $aliases"
            params["aliases"] = props["aliases"]
        if props.get("subtype"):
            query += "\n        SET n.subtype = $subtype"
            params["subtype"] = props["subtype"]
        if props.get("domain"):
            query += "\n        SET n.domain = $domain"
            params["domain"] = props["domain"]
        if props.get("definition"):
            query += "\n        SET n.definition = $definition"
            params["definition"] = props["definition"]
        if props.get("importance"):
            query += "\n        SET n.importance = $importance"
            params["importance"] = props["importance"]

        query += "\n        RETURN n.id AS id"

        records = await self._db.execute_write_for_user(query, params)
        return records[0]["id"] if records else entity_id

    # ------------------------------------------------------------------
    # 3d. Write Explicit Edges (batched)
    # ------------------------------------------------------------------

    async def _create_explicit_edges_batch(
        self, relations: list[RelationInfo],
    ) -> list[str]:
        """Batch create explicit edges with semantic labels (V1.1: extended edge types).

        Edge type mapping:
        - uses/contains/derives_from/implements → EXPLICIT
        - causes → CAUSES
        - precedes → PRECEDES
        - is_a → IS_A
        - contradicts → CONTRADICTS
        - analogous_to → ANALOGOUS_TO
        """
        if not relations:
            return []

        # V1.1: Semantic edge label mapping
        SEMANTIC_EDGE_LABELS = {"causes", "precedes", "is_a", "contradicts", "analogous_to"}
        SYMMETRIC_EDGES = {"analogous_to"}  # Edges that should be created in both directions

        edge_data = []
        for r in relations:
            edge_type = r.type.lower().replace(" ", "_")
            if edge_type in SEMANTIC_EDGE_LABELS:
                neo4j_label = edge_type.upper()
            else:
                neo4j_label = "EXPLICIT"

            edge_data.append({
                "from_id": r.from_entity.replace(" ", "_"),
                "to_id": r.to_entity.replace(" ", "_"),
                "edge_type": edge_type,
                "neo4j_label": neo4j_label,
                "context": r.evidence,
            })

            # V1.1: Auto-create reverse edges for symmetric relations
            if edge_type in SYMMETRIC_EDGES:
                edge_data.append({
                    "from_id": r.to_entity.replace(" ", "_"),
                    "to_id": r.from_entity.replace(" ", "_"),
                    "edge_type": edge_type,
                    "neo4j_label": neo4j_label,
                    "context": f"对称关系: {r.evidence}",
                })

        query = """
        UNWIND $edges AS edge
        MATCH (a), (b) WHERE a.id = edge.from_id AND b.id = edge.to_id
          AND a.user_id = $_user_id AND b.user_id = $_user_id
        CALL apoc.merge.relationship(a, edge.neo4j_label,
            {type: edge.edge_type}, {},
            b, {}
        ) YIELD rel
        SET rel.context = edge.context,
            rel.created_at = datetime()
        RETURN edge.from_id + '-[' + edge.edge_type + ']->' + edge.to_id AS edge_id
        """
        try:
            records = await self._db.execute_write_for_user(query, {"edges": edge_data})
            return [r["edge_id"] for r in records]
        except Exception as exc:
            # Fallback: use MERGE without APOC for environments without APOC
            logger.debug("APOC-based edge creation failed, using MERGE fallback: %s", exc)
            return await self._create_explicit_edges_batch_merge(edge_data)

    async def _create_explicit_edges_batch_merge(
        self, edge_data: list[dict[str, Any]],
    ) -> list[str]:
        """V1.1: Fallback edge creation using MERGE (no APOC dependency)."""
        if not edge_data:
            return []
        # Group by neo4j_label for separate MERGE queries
        result_ids: list[str] = []
        for edge in edge_data:
            query = f"""
            MATCH (a), (b) WHERE a.id = $from_id AND b.id = $to_id
              AND a.user_id = $_user_id AND b.user_id = $_user_id
            MERGE (a)-[r:{edge['neo4j_label']} {{type: $edge_type}}]->(b)
            SET r.context = $context,
                r.created_at = datetime()
            RETURN a.id + '-[' + $edge_type + ']->' + b.id AS edge_id
            """
            try:
                records = await self._db.execute_write_for_user(query, {
                    "from_id": edge["from_id"],
                    "to_id": edge["to_id"],
                    "edge_type": edge["edge_type"],
                    "context": edge.get("context", ""),
                })
                for r in records:
                    result_ids.append(r.get("edge_id", ""))
            except Exception as exc:
                logger.warning("MERGE edge creation failed for %s->%s: %s",
                               edge["from_id"], edge["to_id"], exc)
        return result_ids

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
        """Use LLM to discover implicit relations with 2-hop context (V1.1 enhanced)."""
        if not new_node_ids:
            return []

        # 1. Build context: new nodes + 2-hop neighbors + bridge entities
        context_parts = []
        for node_id in new_node_ids:
            node = await self._db.get_node_by_id(node_id)
            if node:
                subtype_hint = f"({node.get('subtype', 'unknown')})" if node.get('subtype') else ""
                context_parts.append(
                    f"【{node.get('name', node_id)}】{subtype_hint}: {node.get('summary', '无摘要')}"
                )

        # 2. Get 2-hop neighbors with path descriptions
        two_hop_records = await self._db.execute_read_for_user(
            """
            MATCH (n)-[r1]->(m)-[r2]->(o)
            WHERE n.id IN $ids
              AND m.id <> n.id AND o.id <> n.id AND o.id <> m.id
              AND n.user_id = $_user_id AND m.user_id = $_user_id AND o.user_id = $_user_id
            RETURN DISTINCT
                n.name AS source_name,
                COALESCE(r1.type, type(r1)) AS rel1_semantic,
                m.name AS mid_name, m.summary AS mid_summary,
                COALESCE(r2.type, type(r2)) AS rel2_semantic,
                o.name AS target_name, o.summary AS target_summary
            LIMIT 50
            """,
            {"ids": new_node_ids},
        )

        path_descriptions: list[str] = []
        seen_paths: set[str] = set()
        for r in two_hop_records:
            key = f"{r['source_name']}-{r['rel1_semantic']}-{r['mid_name']}-{r['rel2_semantic']}-{r['target_name']}"
            if key not in seen_paths:
                seen_paths.add(key)
                path_descriptions.append(
                    f"  {r['source_name']} -[{r['rel1_semantic']}]-> "
                    f"{r['mid_name']} -[{r['rel2_semantic']}]-> {r['target_name']}"
                )

        # 3. Bridge entities (high PageRank nodes connecting different areas)
        bridge_records = await self._db.execute_read_for_user(
            """
            MATCH (n) WHERE n.id IN $ids AND n.user_id = $_user_id
            MATCH (b) WHERE b.page_rank > 0.05 AND b.user_id = $_user_id AND NOT b.id IN $ids
            MATCH path = (n)-[*1..2]-(b)
            RETURN DISTINCT b.name AS name, b.summary AS summary, b.page_rank AS page_rank
            ORDER BY b.page_rank DESC LIMIT 10
            """,
            {"ids": new_node_ids},
        )

        # 4. Build user prompt
        user_prompt = "【新节点】\n" + "\n".join(context_parts) + "\n\n"
        if path_descriptions:
            user_prompt += "【2-hop 图谱路径】\n" + "\n".join(path_descriptions) + "\n\n"
        if bridge_records:
            user_prompt += "【高价值桥接节点】\n" + "\n".join(
                f"- {b['name']}: {b.get('summary', '')}" for b in bridge_records
            ) + "\n\n"
        user_prompt += "请发现隐式关系并输出 JSON 数组。如果没有发现隐式关系，输出空数组 []。"

        # V1.1: Inject few-shot examples
        fewshot = load_fewshot("implicit_fewshot.txt")
        effective_system = IMPLICIT_SYSTEM_PROMPT
        if fewshot:
            effective_system = IMPLICIT_SYSTEM_PROMPT + "\n\n" + fewshot

        try:
            result = await self._llm.chat_json(effective_system, user_prompt, model=self._reasoning_model)
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
            confidence = float(item.get("confidence", 0.5))
            if confidence < 0.3:  # V1.1: reject low-confidence relations
                continue
            relations.append(ImplicitRelation(
                source=item.get("source", ""),
                target=item.get("target", ""),
                type=rel_type,
                confidence=confidence,
                evidence=item.get("evidence", ""),
            ))

        # 5. Validate relations before returning (V1.1: post-validation)
        validated = await self._validate_implicit_relations(relations)
        return validated

    async def _validate_implicit_relations(
        self, relations: list[ImplicitRelation],
    ) -> list[ImplicitRelation]:
        """V1.1: Post-validate implicit relations — check node existence and deduplicate."""
        if not relations:
            return []
        validated: list[ImplicitRelation] = []
        seen_pairs: set[tuple[str, str]] = set()

        for rel in relations:
            # Skip empty source/target
            if not rel.source or not rel.target:
                continue

            from_id = rel.source.replace(" ", "_")
            to_id = rel.target.replace(" ", "_")

            # Skip self-referencing
            if from_id == to_id:
                continue

            # Deduplicate by source-target pair
            pair = (from_id, to_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # Verify both nodes exist
            node_exists = await self._db.execute_read_for_user(
                "MATCH (a), (b) WHERE a.id = $from_id AND b.id = $to_id "
                "AND a.user_id = $_user_id AND b.user_id = $_user_id "
                "RETURN a.id AS a_id, b.id AS b_id",
                {"from_id": from_id, "to_id": to_id},
            )
            if not node_exists:
                logger.debug("Implicit relation skipped: node not found %s->%s", rel.source, rel.target)
                continue

            # Check no existing EXPLICIT edge already
            existing = await self._db.execute_read_for_user(
                "MATCH (a)-[r:EXPLICIT]->(b) WHERE a.id = $from_id AND b.id = $to_id "
                "AND a.user_id = $_user_id AND b.user_id = $_user_id "
                "RETURN count(r) AS cnt",
                {"from_id": from_id, "to_id": to_id},
            )
            if existing and existing[0]["cnt"] > 0:
                logger.debug("Implicit relation skipped: EXPLICIT edge already exists %s->%s", rel.source, rel.target)
                continue

            validated.append(rel)

        return validated

    # ------------------------------------------------------------------
    # 3f. Write Implicit Edges
    # ------------------------------------------------------------------

    async def _create_implicit_edge(self, rel: ImplicitRelation) -> None:
        """Write an IMPLICIT relationship to Neo4j.

        V1.1: Symmetric relations (trade_off, analogous_to) get auto-created reverse edges.
        """
        from_id = rel.source.replace(" ", "_")
        to_id = rel.target.replace(" ", "_")

        query = """
        MATCH (a), (b) WHERE a.id = $from_id AND b.id = $to_id
          AND a.user_id = $_user_id AND b.user_id = $_user_id
        MERGE (a)-[r:IMPLICIT {type: $edge_type}]->(b)
        SET r.confidence = $confidence,
            r.evidence = $evidence,
            r.discovered_at = datetime()
        """
        try:
            await self._db.execute_write_for_user(query, {
                "from_id": from_id,
                "to_id": to_id,
                "edge_type": rel.type.value,
                "confidence": rel.confidence,
                "evidence": rel.evidence,
            })
        except Exception as exc:
            logger.warning("Failed to create implicit edge %s→%s: %s", from_id, to_id, exc)

        # V1.1: Auto-create reverse edges for symmetric relations
        if rel.type in (ImplicitRelationType.TRADE_OFF, ImplicitRelationType.ANALOGOUS_TO):
            reverse_query = """
            MATCH (a), (b) WHERE a.id = $to_id AND b.id = $from_id
              AND a.user_id = $_user_id AND b.user_id = $_user_id
            MERGE (a)-[r:IMPLICIT {type: $edge_type}]->(b)
            SET r.confidence = $confidence,
                r.evidence = $evidence,
                r.discovered_at = datetime()
            """
            try:
                await self._db.execute_write_for_user(reverse_query, {
                    "from_id": from_id,
                    "to_id": to_id,
                    "edge_type": rel.type.value,
                    "confidence": rel.confidence,
                    "evidence": f"对称关系: {rel.evidence}",
                })
            except Exception as exc:
                logger.debug("Failed to create reverse implicit edge %s→%s: %s", to_id, from_id, exc)

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
        MATCH (n) WHERE n.id = node_id AND n.user_id = $_user_id
        OPTIONAL MATCH (other)-[r]->(n)
        WITH n, count(r) AS in_degree
        SET n.page_rank = CASE WHEN in_degree > 0
                          THEN toFloat(in_degree) / 10.0
                          ELSE 0.01 END
        """
        try:
            await self._db.execute_write_for_user(query, {"node_ids": affected_nodes})
        except Exception as exc:
            logger.warning("Batch PageRank update failed: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_related_summary(self, entity_name: str) -> str:
        """Get summary of related nodes for LLM context."""
        records = await self._db.execute_read_for_user(
            "MATCH (n) WHERE (n.name CONTAINS $name OR n.id CONTAINS $name) "
            "AND n.user_id = $_user_id "
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
