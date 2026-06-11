"""Step 2: Analyze and classify material using LLM."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.database import Neo4jDatabase
from app.ingest.fewshot import load_fewshot
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


def _get_entity_cap(content_length: int) -> int:
    """Dynamic entity cap based on content length."""
    if content_length < 500:
        return 5
    elif content_length < 2000:
        return 10
    elif content_length < 8000:
        return 20
    return 30


SYSTEM_PROMPT = """你是一个知识素材分析器。你的任务是对用户提交的原始知识内容进行结构化分析。

## 一、素材类型分类（五型决策树判定）

请按以下优先级顺序进行判定，并输出判定理由：

1. 内容是否以「vs」、「对比」、「区别」、「相比」、「优劣」为核心结构？
   → 若是 → **comparative**（对比型），判定理由引用对比双方

2. 内容是否包含因果推理链条（「导致」「因为...所以」「推导」「影响链」）？
   → 若是且以逻辑推导为核心 → **relational**（关系型）

3. 内容是否来自第一人称实践描述（「我」「我们」「踩坑」「经验」「发现」）？
   → 若是且以个人/团队经历为核心 → **experiential**（经验型）

4. 内容是阐述理论/方法论/框架/范式，还是描述具体事实/数据/定义？
   → 前者 → **conceptual**（概念型）
   → 后者 → **factual**（事实型）

## 二、实体提取指南

### 有效实体的四个必要条件（全部满足）
1. **具体性**：可以独立成篇进行解释，不是描述性短语
2. **可定义性**：能用一句话清晰定义其本质（\"X 是什么\"）
3. **知识性**：属于知识单元而非临时性/情境性表达
4. **稳定性**：不是只出现一次的偶然提及

### 禁止提取的实体类型
- 市场评论/投资话术：\"创新高\"、\"杀估值\"、\"高位震荡\"、\"赛道\"、\"风口\"
- 趋势描述：\"温和扩张\"、\"分化拉大\"、\"竞争加剧\"、\"价格下行\"、\"产能扩张\"
- 修饰性概念：没有实质内涵的形容词/状态词
- 过于宽泛：无法给出精确定义的上位概念（如\"技术\"、\"互联网\"、\"AI\"应具体化）
- 1-2字的极短中文词（除非是业界公认缩写如RAG/GPT）

### 实体数量规则（动态上限）
- 素材 < 500 字符：最多 5 个实体
- 素材 < 2000 字符：最多 10 个实体
- 素材 < 8000 字符：最多 20 个实体
- 素材 >= 8000 字符：最多 30 个实体
- 超过上限时，优先保留 importance 最高的

### 实体信息要求
对每个实体，提供以下字段：
- **name**：实体名称（使用最通用/规范的名称）
- **aliases**：已知别名列表，如 [\"K8s\", \"kube\"] 是 Kubernetes 的别名
- **subtype**：子类型（technology/person/organization/product/event/theory/methodology/paradigm/practice/case_study）
- **domain**：所属知识领域（如 AI/database/architecture/finance/management）
- **importance**：重要性 1-10（10=该素材的核心主题，5=重要配角，1=顺便提及）
- **definition**：一句话定义（回答\"它是什么？\"）
- **exists**：是否可能已存在于知识库（业界通用术语→true，个人笔记→false）
- **exists_reason**：存在性判断依据（一句话，说明为什么判断已存在/不存在）

## 三、关系提取规则

- 只提取素材中**明确提及或强烈暗示**的关系
- 关系类型使用标准术语：uses/contains/contrasts_with/causes/derives_from/implements/evolves_to
- 每条关系必须附带 **evidence**（素材中支持该关系的原文片段≥10字）

## 四、冲突检测

检测素材内容与现有知识库的潜在冲突：
- **factual_conflict**：事实层面的矛盾（如\"X 是 Y\" vs 已有记录\"X 不是 Y\"）
- **opinion_conflict**：观点/评价层面的分歧（两种观点可共存）
- **temporal_update**：信息时效性更新（新信息替换旧信息，标注新旧内容）
- 不确定的冲突请降低 confidence 而非猜测

## 五、输出 JSON 结构

{
  \"type\": \"factual|conceptual|experiential|comparative|relational\",
  \"type_confidence\": 0.0-1.0,
  \"classification_reason\": \"判定理由（一句话，引用素材中的关键信号）\",
  \"entities\": [
    {
      \"name\": \"实体名\",
      \"aliases\": [\"别名1\"],
      \"subtype\": \"technology\",
      \"domain\": \"AI\",
      \"importance\": 8,
      \"definition\": \"一句话定义\",
      "exists": true/false,
      "exists_reason": "判断依据"
    }
  ],
  \"relations\": [
    {\"from\": \"实体A\", \"to\": \"实体B\", \"type\": \"uses\", \"evidence\": \"原文依据（≥10字）\"}
  ],
  \"conflicts\": [
    {\n      \"node\": \"节点名\",
      \"field\": \"字段\",
      \"existing\": \"已有内容\",
      \"new\": \"新内容\",
      \"type\": \"factual_conflict|opinion_conflict|temporal_update\",
      \"confidence\": 0.0-1.0,
      \"suggestion\": \"解决建议\"
    }
  ],
  \"gaps\": [\"缺失的知识点\"],
  \"compile_suggestion\": \"编译建议（如何将素材整合进知识库）\"
}

## 六、重要提醒

- 只输出 JSON，不要输出任何其他文字
- 不确定的判定请降低 confidence 并注明原因，而非猜测
- 实体名称不要包含括号注释（如不要写\"Neo4j(图数据库)\"，只写\"Neo4j\"）
- 每个 entity 的 definition 字段不能为空——必须写出一句话定义"""


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

        # 2. Build user prompt with dynamic entity cap
        content_len = len(raw_content)
        entity_cap = _get_entity_cap(content_len)
        user_prompt = f"【素材内容】\n{raw_content[:8000]}\n\n"
        if nodes_summary:
            user_prompt += f"【现有知识库节点摘要】\n{nodes_summary[:2000]}\n\n"
        user_prompt += f"请分析以上素材并输出 JSON。注意：素材约 {content_len} 字符，最多提取 {entity_cap} 个实体。"

        # 3. Call LLM with optional few-shot injection (V1.1)
        fewshot = load_fewshot("analyze_fewshot.txt")
        effective_system = SYSTEM_PROMPT
        if fewshot:
            effective_system = SYSTEM_PROMPT + "\n\n" + fewshot

        try:
            result = await self._llm.chat_json(effective_system, user_prompt, model=self._model)
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

        # 5. V1.1: Three-tier entity matching with LLM pre-judgment cross-validation
        raw_entities_data = result.get("entities", [])
        entities = await self._match_entities_tiered(raw_entities_data)

        # 5b. Post-filter: remove low-quality entities and enforce dynamic cap
        entities = self._filter_entities(entities, result, content_len)

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

        # 8. Determine material type with confidence
        type_str = result.get("type", "factual")
        try:
            material_type = MaterialType(type_str)
        except ValueError:
            material_type = MaterialType.FACTUAL

        type_confidence = float(result.get("type_confidence", 0.5))
        classification_reason = result.get("classification_reason", "")

        return AnalysisReport(
            type=material_type,
            entities=entities,
            relations=relations,
            conflicts=conflicts,
            gaps=result.get("gaps", []),
            compile_suggestion=result.get("compile_suggestion", ""),
            type_confidence=type_confidence,
            classification_reason=classification_reason,
        )

    async def _match_entities_tiered(self, raw_entities: list[dict]) -> list[EntityInfo]:
        """V1.1: Three-tier entity matching with LLM pre-judgment cross-validation.

        For each entity extracted by LLM:
        1. Use three-tier matching (exact → alias → semantic)
        2. Compare LLM's exists_guess with Neo4j reality
        3. Log mismatches for diagnostic purposes
        """
        entities: list[EntityInfo] = []
        mismatch_count = 0

        for ent_data in raw_entities:
            name = ent_data.get("name", "").strip()
            if not name:
                continue

            # Parse LLM's fields
            aliases = ent_data.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",") if a.strip()]
            subtype = ent_data.get("subtype", "")
            domain = ent_data.get("domain", "")
            definition = ent_data.get("definition", "")
            importance = int(ent_data.get("importance", 5))
            exists_guess = bool(ent_data.get("exists", False))
            exists_reason = ent_data.get("exists_reason", "")

            # Three-tier matching
            match_result = await self._db.match_entity_tiered(name, aliases)
            node_id = match_result.get("node_id")
            matched_by = match_result.get("matched_by", "new")
            exists_actual = node_id is not None

            # V1.1: Cross-validation — LLM pre-judgment vs Neo4j reality
            if exists_guess and not exists_actual:
                # LLM thought entity exists but not found → flag for review
                mismatch_count += 1
                logger.debug(
                    "Entity existence mismatch [false_positive]: '%s' — LLM says exists (%s), "
                    "but not found in Neo4j (matched_by=%s)",
                    name, exists_reason, matched_by,
                )
            elif not exists_guess and exists_actual:
                # LLM thought entity is new but found in Neo4j → potential duplicate
                mismatch_count += 1
                logger.info(
                    "Entity existence mismatch [false_negative]: '%s' — LLM says new (%s), "
                    "but found via %s match (node_id=%s)",
                    name, exists_reason, matched_by, node_id,
                )

            entities.append(EntityInfo(
                name=name,
                exists=exists_actual,
                node_id=node_id,
                aliases=aliases,
                subtype=subtype,
                domain=domain,
                definition=definition,
                importance=importance,
                exists_guess=exists_guess,
                exists_reason=exists_reason,
                matched_by=matched_by,
            ))

        if mismatch_count > 0:
            logger.info(
                "Entity existence cross-validation: %d/%d mismatches detected",
                mismatch_count, len(entities),
            )

        return entities

    # ------------------------------------------------------------------
    # Legacy methods (kept for backward compatibility)
    # ------------------------------------------------------------------

    def _filter_entities(self, entities: list[EntityInfo], raw_result: dict, content_length: int = 4000) -> list[EntityInfo]:
        """Post-filter: remove low-quality entities and enforce dynamic cap."""
        # V1.1: hardcoded blacklist kept for backward compatibility;
        # the new Prompt's decision tree handles most generic phrase filtering upstream
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

        # Sort by importance, enforce dynamic cap
        importance_map: dict[str, int] = {}
        for e in raw_result.get("entities", []):
            importance_map[e.get("name", "")] = e.get("importance", 5)
        filtered.sort(key=lambda e: importance_map.get(e.name, 5), reverse=True)

        cap = _get_entity_cap(content_length)
        return filtered[:cap]

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
