"""Step 2: Analyze and classify material using LLM."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.database import Neo4jDatabase
from app.llm import LLMClient
from app.models import (
    AnalysisReport,
    ConflictInfo,
    ConflictType,
    EntityInfo,
    MaterialType,
    RelationInfo,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个知识素材分析器。请分析以下素材内容，提取有价值的知识实体并输出 JSON。

## 实体提取规则（严格遵守）

1. **数量限制**：最多提取 10 个实体。优先选择最重要、最具体的。
2. **有效实体**：必须是具体的、可独立成篇的知识单元：
   - 具体技术/工具/框架（如：Kubernetes, React, Neo4j）
   - 明确的概念/理论（如：CAP定理, 微服务架构）
   - 具体的人/组织/产品（如：OpenAI, GPT-4）
   - 具体的方法论/模式（如：TDD, 敏捷开发）
3. **禁止提取**（这些不是实体）：
   - 抽象形容词或状态："创新高"、"温和扩张"、"杀估值"
   - 通用动词短语："数据分析"（除非是具体方法论）
   - 过于宽泛的概念："技术"、"互联网"、"AI"（太泛，应具体化）
   - 市场评论、情绪判断、趋势描述性短语
4. **检验标准**：每个实体必须能回答"它是什么？"——如果不能写出一段定义，则不是合格实体。

## 输出 JSON 结构
{
  "type": "factual|conceptual|experiential|comparative|relational",
  "entities": [
    {"name": "实体名", "importance": 8, "exists": true/false, "node_id": "节点ID或null"}
  ],
  "relations": [
    {"from": "实体A", "to": "实体B", "type": "关系类型", "evidence": "依据"}
  ],
  "conflicts": [
    {"node": "节点名", "field": "字段", "existing": "已有内容", "new": "新内容", "type": "factual_conflict|opinion_conflict|temporal_update"}
  ],
  "gaps": ["缺失的知识点"],
  "compile_suggestion": "编译建议"
}

## 类型分类标准
- factual（事实型）：数据、定义、规格、日期、数字
- conceptual（概念型）：理论、方法论、框架、范式
- experiential（经验型）：实践发现、踩坑、个人经验
- comparative（对比型）：vs、比较、优缺点、区别
- relational（关系型）：导致、因为、推导、因果

注意：只输出 JSON，不要其他内容。"""


class Analyzer:
    """Analyze and classify material using LLM + Neo4j entity checking."""

    def __init__(self, llm: LLMClient, db: Neo4jDatabase, model: str | None = None) -> None:
        self._llm = llm
        self._db = db
        self._model = model

    async def analyze(self, raw_content: str, raw_path: str) -> AnalysisReport:
        """Run full analysis: LLM classification + entity verification + conflict detection."""
        # 1. Get Neo4j nodes summary for context
        nodes_summary = await self._db.get_nodes_summary()

        # 2. Build user prompt
        user_prompt = f"【素材内容】\n{raw_content[:4000]}\n\n"
        if nodes_summary:
            user_prompt += f"【现有知识库节点摘要】\n{nodes_summary[:2000]}\n\n"
        user_prompt += "请分析以上素材并输出 JSON。"

        # 3. Call LLM
        try:
            result = await self._llm.chat_json(SYSTEM_PROMPT, user_prompt, model=self._model)
        except Exception as exc:
            logger.error("LLM analysis failed: %s", exc)
            # Fallback: basic factual classification
            return AnalysisReport(
                type=MaterialType.FACTUAL,
                entities=[],
                relations=[],
                conflicts=[],
                gaps=[f"LLM分析失败: {exc}"],
                compile_suggestion="LLM调用失败，使用默认事实型编译策略",
            )

        # 4. Parse result
        if result.get("_parse_error"):
            logger.warning("LLM output was not valid JSON, using fallback")
            return self._parse_fallback(result.get("raw", ""))

        # 5. Verify entity existence in Neo4j (batched — PERF-01 fix)
        raw_entities = [
            {"name": ent.get("name", "")}
            for ent in result.get("entities", [])
            if ent.get("name")
        ]
        existence_map = await self._batch_check_entity_existence(
            [e["name"] for e in raw_entities]
        )
        entities = []
        for item in raw_entities:
            name = item["name"]
            node_id = existence_map.get(name)
            entities.append(EntityInfo(
                name=name,
                exists=node_id is not None,
                node_id=node_id,
            ))

        # 5b. Post-filter: remove low-quality entities and enforce cap
        entities = self._filter_entities(entities, result)

        # 6. Parse relations
        relations = []
        for rel in result.get("relations", []):
            relations.append(RelationInfo(
                from_entity=rel.get("from", ""),
                to_entity=rel.get("to", ""),
                type=rel.get("type", "related_to"),
                evidence=rel.get("evidence", ""),
            ))

        # 7. Parse conflicts
        conflicts = []
        for c in result.get("conflicts", []):
            try:
                conflict_type = ConflictType(c.get("type", "temporal_update"))
            except ValueError:
                conflict_type = ConflictType.TEMPORAL_UPDATE
            conflicts.append(ConflictInfo(
                node=c.get("node", ""),
                field=c.get("field", ""),
                existing=c.get("existing", ""),
                new=c.get("new", ""),
                conflict_type=conflict_type,
            ))

        # 8. Determine material type
        type_str = result.get("type", "factual")
        try:
            material_type = MaterialType(type_str)
        except ValueError:
            material_type = MaterialType.FACTUAL

        return AnalysisReport(
            type=material_type,
            entities=entities,
            relations=relations,
            conflicts=conflicts,
            gaps=result.get("gaps", []),
            compile_suggestion=result.get("compile_suggestion", ""),
        )

    async def _batch_check_entity_existence(self, names: list[str]) -> dict[str, str | None]:
        """Batch check entity existence in Neo4j. Returns {name: node_id_or_None}."""
        if not names:
            return {}
        result: dict[str, str | None] = {n: None for n in names}
        normalized_map: dict[str, str] = {}
        for n in names:
            normalized_map[n.replace(" ", "_")] = n

        all_ids = list(set(names + list(normalized_map.keys())))
        records = await self._db.execute_read(
            "MATCH (n) WHERE n.id IN $ids OR n.name IN $names RETURN n.id AS id, n.name AS name",
            {"ids": all_ids, "names": names},
        )
        # Build reverse lookup: id -> id, name -> id, normalized_id -> id
        id_lookup: dict[str, str] = {}
        for r in records:
            nid = r["id"]
            nname = r.get("name", "")
            id_lookup[nid] = nid
            id_lookup[nname] = nid
            id_lookup[nid.replace(" ", "_")] = nid

        for name in names:
            if name in id_lookup:
                result[name] = id_lookup[name]
            elif name.replace(" ", "_") in id_lookup:
                result[name] = id_lookup[name.replace(" ", "_")]
        return result

    async def _check_entity_existence(self, entity_name: str) -> str | None:
        """Check if entity exists in Neo4j by exact id or name match."""
        # Try exact match on id or name
        records = await self._db.execute_read(
            "MATCH (n) WHERE n.id = $name OR n.name = $name RETURN n.id AS id LIMIT 1",
            {"name": entity_name},
        )
        if records:
            return records[0]["id"]
        # Also check normalized id (spaces → underscores) to match graph_process convention
        normalized = entity_name.replace(" ", "_")
        if normalized != entity_name:
            records = await self._db.execute_read(
                "MATCH (n) WHERE n.id = $id RETURN n.id AS id LIMIT 1",
                {"id": normalized},
            )
            if records:
                return records[0]["id"]
        return None

    def _filter_entities(self, entities: list[EntityInfo], raw_result: dict) -> list[EntityInfo]:
        """Post-filter: remove low-quality entities and enforce cap."""
        GENERIC_PHRASES = {
            "创新高", "杀估值", "温和扩张", "叙事", "趋势", "风口",
            "红利", "赛道", "护城河", "破圈", "内卷", "高位震荡",
            "分化拉大", "主升浪", "政策驱动", "业绩驱动", "估值修复",
            "价格下行", "产能扩张", "竞争加剧", "行业周期",
        }

        filtered = []
        for ent in entities:
            name = ent.name.strip()
            if len(name) < 2:
                continue
            if name in GENERIC_PHRASES:
                continue
            if name.replace(" ", "").isdigit():
                continue
            filtered.append(ent)

        # Sort by importance, take top 10
        importance_map: dict[str, int] = {}
        for e in raw_result.get("entities", []):
            importance_map[e.get("name", "")] = e.get("importance", 5)
        filtered.sort(key=lambda e: importance_map.get(e.name, 5), reverse=True)

        return filtered[:10]

    def _parse_fallback(self, raw: str) -> AnalysisReport:
        """Create a minimal analysis report when LLM fails."""
        return AnalysisReport(
            type=MaterialType.FACTUAL,
            entities=[],
            relations=[],
            conflicts=[],
            gaps=["LLM JSON解析失败"],
            compile_suggestion="创建基础实体节点",
        )
