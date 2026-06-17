"""V1.2: EntityResolver — bridges entity extraction and graph retrieval.

The existing architecture had a "throw it over the wall" anti-pattern:
_extract_entities produced raw strings, _factual_retrieve tried to map them
to Neo4j nodes. If extraction produced garbage (e.g., entire Chinese sentence
as one token), retrieval silently returned empty — with no feedback loop.

EntityResolver inserts a resolution layer with multi-tier matching:
  Tier 1: Exact ID/Name match
  Tier 2: CONTAINS substring match
  Tier 3: Fuzzy (trigram/difflib) match for typo-tolerant lookup

Returns EntityResolution with structured feedback on what matched and what
didn't, enabling the pipeline to make informed decisions downstream.
"""

from __future__ import annotations

import difflib
import logging

from app.database import Neo4jDatabase
from app.models import EntityResolution

logger = logging.getLogger(__name__)

# Similarity threshold for fuzzy matching (0.0-1.0)
_FUZZY_THRESHOLD = 0.6


class EntityResolver:
    """Multi-tier entity resolution: exact → CONTAINS → fuzzy → feedback."""

    def __init__(self, db: Neo4jDatabase) -> None:
        self._db = db

    async def resolve(self, candidates: list[str]) -> EntityResolution:
        """Resolve candidate entity strings to confirmed Neo4j node names.

        Args:
            candidates: Entity name candidates from jieba extraction.

        Returns:
            EntityResolution with resolved names, unresolved candidates,
            and search suggestions for the user.
        """
        if not candidates:
            return EntityResolution()

        resolved: list[str] = []
        unresolved: list[str] = []
        suggestions: list[str] = []

        for term in candidates:
            # Tier 1+2: Exact + CONTAINS match (single query)
            records = await self._db.execute_read_for_user(
                """
                MATCH (n)
                WHERE (n:Entity OR n:Concept)
                  AND n.user_id = $_user_id
                  AND (n.id = $term OR n.name = $term
                       OR n.id CONTAINS $term OR n.name CONTAINS $term)
                RETURN n.name AS name
                LIMIT 3
                """,
                {"term": term},
            )
            if records:
                resolved.extend(r["name"] for r in records if r.get("name"))
                continue

            # Tier 3: Fuzzy match — find closest node name
            fuzzy_match = await self._fuzzy_lookup(term)
            if fuzzy_match:
                resolved.append(fuzzy_match)
                logger.debug("Fuzzy match: %r → %r", term, fuzzy_match)
                continue

            # No match at any tier
            unresolved.append(term)

        # Generate suggestions for unresolved terms
        if unresolved:
            suggestions = await self._generate_suggestions(unresolved)

        return EntityResolution(
            resolved=list(dict.fromkeys(resolved)),  # deduplicate, preserve order
            unresolved=unresolved,
            suggestions=suggestions,
        )

    async def _fuzzy_lookup(self, term: str, limit: int = 20) -> str | None:
        """Find the closest Neo4j node name to the given term using difflib.

        Fetches up to `limit` node names and computes SequenceMatcher ratio
        against each. Returns the best match above _FUZZY_THRESHOLD.
        """
        try:
            records = await self._db.execute_read_for_user(
                """
                MATCH (n)
                WHERE (n:Entity OR n:Concept) AND n.user_id = $_user_id
                RETURN n.name AS name
                LIMIT $limit
                """,
                {"limit": limit},
            )
        except Exception:
            logger.warning("Fuzzy lookup query failed for term=%r", term, exc_info=True)
            return None

        if not records:
            return None

        best_match: str | None = None
        best_score: float = 0.0

        for r in records:
            name = r.get("name", "")
            if not name:
                continue
            score = difflib.SequenceMatcher(None, term.lower(), name.lower()).ratio()
            if score > best_score:
                best_score = score
                best_match = name

        if best_match and best_score >= _FUZZY_THRESHOLD:
            return best_match
        return None

    async def _generate_suggestions(self, unresolved: list[str]) -> list[str]:
        """Generate search suggestions for entities that couldn't be resolved."""
        if not unresolved:
            return []
        suggestions: list[str] = []
        for term in unresolved[:3]:  # Limit suggestions
            suggestions.append(f'尝试搜索「{term}」的同义词或英文名')
        return suggestions
