"""Lint checker: quality checks for the knowledge graph.

Implementation status (4/7 checks implemented):
  ✅ Orphan nodes (check_orphan_nodes)
  ✅ Duplicate nodes (check_duplicate_nodes) — name similarity detection
  ✅ Low confidence implicit edges (check_low_confidence_edges)
  ✅ Broken relations (check_broken_relations) — dangling BELONGS_TO edges
  ❌ Factual contradiction detection (TODO)
  ❌ Outdated content detection (TODO)
  ❌ Implicit entity detection (TODO)
"""

from __future__ import annotations

import logging
from typing import Any

from app.database import Neo4jDatabase
from app.models import LintReport

logger = logging.getLogger(__name__)


class LintChecker:
    """Run quality checks on the knowledge graph."""

    def __init__(self, db: Neo4jDatabase) -> None:
        self._db = db

    async def run_all_checks(self, auto_fix: bool = False) -> LintReport:
        """Run all lint checks and optionally auto-fix issues."""
        report = LintReport()

        # 1. Orphan nodes
        report.orphan_nodes = await self.check_orphan_nodes()

        # 2. Duplicate nodes
        report.duplicate_groups = await self.check_duplicate_nodes()

        # 3. Low confidence implicit edges
        report.low_confidence_edges = await self.check_low_confidence_edges(threshold=0.3)

        # 4. Broken relations (edges pointing to non-existent nodes)
        report.broken_relations = await self.check_broken_relations()

        # Auto-fix
        if auto_fix:
            fixed = 0
            fixed += await self.fix_broken_relations(report.broken_relations)
            fixed += await self.merge_duplicate_nodes(report.duplicate_groups)
            report.fixed_count = fixed

        return report

    async def check_orphan_nodes(self) -> list[dict[str, Any]]:
        """Find nodes with no relationships."""
        records = await self._db.execute_read(
            """
            MATCH (n) WHERE (n:Entity OR n:Concept) AND NOT (n)--()
            RETURN n.id AS id, n.name AS name, n.summary AS summary, labels(n) AS labels
            """
        )
        return [dict(r) for r in records]

    async def check_duplicate_nodes(self) -> list[dict[str, Any]]:
        """Find potentially duplicate nodes by name similarity."""
        records = await self._db.execute_read(
            """
            MATCH (a), (b)
            WHERE id(a) < id(b)
              AND (a:Entity OR a:Concept) AND (b:Entity OR b:Concept)
              AND (a.name CONTAINS b.name OR b.name CONTAINS a.name)
              AND a.name <> b.name
            RETURN a.id AS id_a, a.name AS name_a,
                   b.id AS id_b, b.name AS name_b
            LIMIT 20
            """
        )
        # Group by similarity
        groups = []
        seen = set()
        for r in records:
            key = tuple(sorted([r["id_a"], r["id_b"]]))
            if key not in seen:
                seen.add(key)
                groups.append({
                    "nodes": [
                        {"id": r["id_a"], "name": r["name_a"]},
                        {"id": r["id_b"], "name": r["name_b"]},
                    ],
                })
        return groups

    async def check_low_confidence_edges(self, threshold: float = 0.3) -> list[dict[str, Any]]:
        """Find implicit edges with low confidence."""
        records = await self._db.execute_read(
            """
            MATCH (a)-[r:IMPLICIT]->(b)
            WHERE r.confidence < $threshold
            RETURN a.id AS from_id, a.name AS from_name,
                   b.id AS to_id, b.name AS to_name,
                   r.type AS rel_type, r.confidence AS confidence, r.evidence AS evidence
            ORDER BY r.confidence ASC
            """,
            {"threshold": threshold},
        )
        return [dict(r) for r in records]

    async def check_broken_relations(self) -> list[dict[str, Any]]:
        """Check for broken references (edges referencing non-existent nodes)."""
        # In Neo4j, edges always connect existing nodes, so this checks for
        # BELONGS_TO edges pointing to non-existent clusters
        records = await self._db.execute_read(
            """
            MATCH (n)-[r:BELONGS_TO]->(c)
            WHERE NOT EXISTS { MATCH (cl:Cluster) WHERE cl.id = c.id }
            RETURN n.id AS node_id, n.name AS node_name, c.id AS cluster_id
            """
        )
        return [dict(r) for r in records]

    async def fix_broken_relations(self, broken: list[dict[str, Any]]) -> int:
        """Remove broken BELONGS_TO edges."""
        fixed = 0
        for item in broken:
            try:
                await self._db.execute_write(
                    """
                    MATCH (n)-[r:BELONGS_TO]->()
                    WHERE n.id = $node_id
                    DELETE r
                    """,
                    {"node_id": item["node_id"]},
                )
                fixed += 1
            except Exception as exc:
                logger.warning("Fix broken relation failed: %s", exc)
        return fixed

    async def merge_duplicate_nodes(self, duplicates: list[dict[str, Any]]) -> int:
        """Merge duplicate nodes (keep the one with more relationships)."""
        merged = 0
        for group in duplicates:
            nodes = group.get("nodes", [])
            if len(nodes) < 2:
                continue
            # Keep first, delete second
            keep_id = nodes[0]["id"]
            remove_id = nodes[1]["id"]
            try:
                await self._db.execute_write(
                    """
                    MATCH (remove) WHERE remove.id = $remove_id
                    MATCH (kept) WHERE kept.id = $keep_id
                    OPTIONAL MATCH (remove)-[r]->(other)
                    WHERE other <> kept
                    WITH remove, kept, collect({type: type(r), target: other}) AS outgoing
                    OPTIONAL MATCH (other2)-[r2]->(remove)
                    WITH remove, kept, outgoing, collect({type: type(r2), source: other2}) AS incoming
                    FOREACH (rel IN outgoing |
                        CREATE (kept)-[:EXPLICIT {type: rel.type}]->(rel.target)
                    )
                    FOREACH (rel IN incoming |
                        CREATE (rel.source)-[:EXPLICIT {type: rel.type}]->(kept)
                    )
                    DETACH DELETE remove
                    """,
                    {"remove_id": remove_id, "keep_id": keep_id},
                )
                merged += 1
            except Exception as exc:
                logger.warning("Merge duplicate failed: %s", exc)
        return merged
