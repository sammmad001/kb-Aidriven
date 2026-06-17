"""Step 1: Query understanding - classify query type and extract entities."""

from __future__ import annotations

import logging

import jieba.posseg as pseg

from app.database import Neo4jDatabase
from app.llm import LLMClient
from app.models import QueryType, QueryUnderstanding

logger = logging.getLogger(__name__)

# Keyword signals for fast rule-based classification
QUERY_SIGNALS: dict[str, list[str]] = {
    "global": ["总结", "全局", "趋势", "所有", "整体", "综合", "全景",
               "summarize", "overview", "trend", "all"],
    "reasoning": ["为什么", "原因", "导致", "因果", "因为", "所以", "推导",
                  "how", "why", "cause", "because"],
    "relational": ["关系", "区别", "对比", "比较", "差异", "影响", "联系", "vs",
                   "relation", "difference", "compare", "vs"],
    "factual": ["是什么", "定义", "全称", "什么叫", "什么是", "多少",
                "what", "who", "when", "define", "meaning"],
}

# LLM prompt for query classification
QUERY_CLASSIFY_SYSTEM = """你是一个查询分析器。分析用户查询，输出 JSON：
{"type": "factual|relational|reasoning|global", "entities": ["实体1", "实体2"], "depth": 0-3}

深度定义：
- 0：单节点事实查询
- 1：关联查询（2-3个实体间的关系）
- 2：多跳推理查询
- 3：全局综合分析

只输出 JSON，不要其他内容。"""


class QueryUnderstander:
    """Classify queries using keyword rules + LLM fallback."""

    def __init__(self, db: Neo4jDatabase, llm: LLMClient, model: str | None = None) -> None:
        self._db = db
        self._llm = llm
        self._model = model

    async def understand(self, question: str) -> QueryUnderstanding:
        """Analyze query: type, entities, depth."""
        # Step 1: Rule-based classification (fast, zero cost)
        rule_result = self._classify_by_rules(question)

        # Step 2: If ambiguous, use LLM
        if self._is_ambiguous(rule_result, question):
            try:
                llm_result = await self._classify_by_llm(question)
                rule_result.update(llm_result)
            except Exception as exc:
                logger.warning("LLM query classification failed, using rules: %s", exc)

        # Step 3: Extract and map entities
        entities = await self._extract_entities(question)

        # Step 4: Build final result
        query_type = QueryType(rule_result.get("type", "factual"))
        depth = rule_result.get("depth", 0)

        return QueryUnderstanding(
            query_type=query_type,
            entities=[e for e in entities if e],
            depth=depth,
            keywords=self._extract_keywords(question),
        )

    # ------------------------------------------------------------------
    # Rule-based classification
    # ------------------------------------------------------------------

    def _classify_by_rules(self, question: str) -> dict:
        """Fast keyword-based query classification."""
        q = question.lower()

        # Check global first (highest priority)
        for kw in QUERY_SIGNALS["global"]:
            if kw in q:
                return {"type": "global", "depth": 3}

        # Check reasoning
        for kw in QUERY_SIGNALS["reasoning"]:
            if kw in q:
                return {"type": "reasoning", "depth": 2}

        # Check relational
        for kw in QUERY_SIGNALS["relational"]:
            if kw in q:
                return {"type": "relational", "depth": 1}

        # Default: factual
        return {"type": "factual", "depth": 0}

    # ------------------------------------------------------------------
    # LLM classification
    # ------------------------------------------------------------------

    async def _classify_by_llm(self, question: str) -> dict:
        """Use LLM for precise query classification."""
        result = await self._llm.chat_json(
            QUERY_CLASSIFY_SYSTEM,
            f"用户查询：{question}",
            model=self._model,
        )
        if result.get("_parse_error"):
            return {}
        return {
            "type": result.get("type", "factual"),
            "depth": result.get("depth", 0),
        }

    # ------------------------------------------------------------------
    # Entity extraction & mapping
    # ------------------------------------------------------------------

    async def _extract_entities(self, question: str) -> list[str]:
        """Extract entity names from query using jieba POS tagging + Neo4j lookup.

        Uses jieba.posseg for Chinese word segmentation with part-of-speech
        filtering to extract meaningful entity candidates (nouns, proper nouns,
        verbal nouns, and English tokens). This replaces the previous naive
        regex r'[\u4e00-\u9fff]+' which treated entire Chinese sentences as
        single tokens.
        """
        # Step 1: jieba POS tagging — extract noun-like terms
        words = pseg.cut(question)
        candidate_terms: list[str] = []
        seen: set[str] = set()
        for w in words:
            # Keep nouns, proper nouns, verbal nouns, verbs, abbreviations, and English words
            if w.flag.startswith(("n", "nr", "ns", "nt", "nz", "v", "vn", "j", "eng")):
                term = w.word.strip()
                if len(term) >= 2 and term not in seen:
                    seen.add(term)
                    candidate_terms.append(term)

        # Step 2: Filter stop words
        _STOP_WORDS = {
            "是什么", "什么", "为什么", "怎么", "如何", "哪些", "是不是",
            "有没有", "能不能", "可不可以", "会不会", "查询", "帮我",
            "所有", "整体", "全局", "关系", "区别", "相关", "的", "和",
            "了", "在", "是", "有", "我", "你", "他", "她", "它", "们",
            "这", "那", "哪", "个", "些", "一下", "一点", "吗", "呢",
            "what", "how", "why", "the", "and", "for", "are", "is", "a", "an",
        }
        valid_terms = [t for t in candidate_terms if t not in _STOP_WORDS]

        if not valid_terms:
            return []

        # Step 3: Neo4j lookup (batched, limit 5 terms)
        terms_slice = valid_terms[:5]
        clauses: list[str] = []
        kw_params: dict[str, str] = {}
        for i, term in enumerate(terms_slice):
            key = f"kw{i}"
            kw_params[key] = term
            clauses.append(f"(n.id CONTAINS ${key} OR n.name CONTAINS ${key})")

        where_clause = " OR ".join(clauses)
        records = await self._db.execute_read_for_user(
            f"""
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND n.user_id = $_user_id
              AND ({where_clause})
            RETURN n.id AS id, n.name AS name
            LIMIT 10
            """,
            kw_params,
        )
        return [r["name"] for r in records if r.get("name")]

    def _extract_keywords(self, question: str) -> list[str]:
        """Extract keywords from query using jieba word segmentation."""
        import jieba
        words = jieba.cut(question)
        return [w for w in words if len(w.strip()) >= 2][:10]

    def _is_ambiguous(self, rule_result: dict, question: str) -> bool:
        """Check if rule-based result is ambiguous.

        Lowered threshold from 30 to 15: short Chinese queries like
        '帮我查询海力士和存储相关的知识' (16 chars) should trigger LLM
        classification when rule-based result is bare factual/depth=0.
        """
        if rule_result.get("depth", 0) == 0 and len(question) > 15:
            return True
        return False
