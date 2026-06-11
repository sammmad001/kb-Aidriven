"""Step 2: Graph retrieval - query Neo4j with adaptive depth."""

from __future__ import annotations

import logging
from typing import Any

from app.database import Neo4jDatabase
from app.models import QueryType, QueryUnderstanding, RetrievalResult

logger = logging.getLogger(__name__)


class GraphRetriever:
    """Retrieve knowledge from Neo4j with adaptive query depth."""

    def __init__(self, db: Neo4jDatabase) -> None:
        self._db = db

    async def retrieve(self, understanding: QueryUnderstanding) -> RetrievalResult:
        """Retrieve data based on query understanding."""
        dispatch = {
            QueryType.FACTUAL: self._factual_retrieve,
            QueryType.RELATIONAL: self._relational_retrieve,
            QueryType.REASONING: self._reasoning_retrieve,
            QueryType.GLOBAL: self._global_retrieve,
        }
        handler = dispatch.get(understanding.query_type, self._factual_retrieve)
        return await handler(understanding)

    # ------------------------------------------------------------------
    # Depth 0: Factual (single node, < 10ms)
    # ------------------------------------------------------------------

    async def _factual_retrieve(self, understanding: QueryUnderstanding) -> RetrievalResult:
        """Retrieve a single node's content."""
        nodes = []
        for entity_name in understanding.entities:
            entity_id = entity_name.replace(" ", "_")
            records = await self._db.execute_read(
                """
                MATCH (n) WHERE n.id = $id OR n.name = $name
                RETURN n.id AS id, n.name AS name, n.content AS content,
                       n.summary AS summary, n.tags AS tags
                LIMIT 1
                """,
                {"id": entity_id, "name": entity_name},
            )
            for r in records:
                nodes.append(dict(r))

        return RetrievalResult(nodes=nodes)

    # ------------------------------------------------------------------
    # Depth 1: Relational (1-hop, < 50ms)
    # ------------------------------------------------------------------

    async def _relational_retrieve(self, understanding: QueryUnderstanding) -> RetrievalResult:
        """Retrieve nodes and their direct relationships."""
        all_nodes = []
        explicit_paths = []

        for entity_name in understanding.entities:
            entity_id = entity_name.replace(" ", "_")

            # Get node data
            node_records = await self._db.execute_read(
                "MATCH (n) WHERE n.id = $id OR n.name = $name "
                "RETURN n.id AS id, n.name AS name, n.content AS content, n.summary AS summary",
                {"id": entity_id, "name": entity_name},
            )
            all_nodes.extend(dict(r) for r in node_records)

            # Get relationships
            rel_records = await self._db.execute_read(
                """
                MATCH (n)-[r]->(m) WHERE n.id = $id OR n.name = $name
                RETURN n.name AS from_name,
                       CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END AS rel_type,
                       m.name AS to_name, m.summary AS to_summary, m.id AS to_id,
                       r.confidence AS confidence
                ORDER BY CASE WHEN r.confidence IS NOT NULL THEN r.confidence ELSE 1.0 END DESC
                """,
                {"id": entity_id, "name": entity_name},
            )
            for r in rel_records:
                explicit_paths.append(dict(r))
                all_nodes.append({"id": r.get("to_id", ""), "name": r.get("to_name", ""),
                                  "summary": r.get("to_summary", "")})

        # Implicit relations
        implicit_rels = await self._get_implicit_relations(understanding.entities)

        return RetrievalResult(
            nodes=self._deduplicate_nodes(all_nodes),
            explicit_paths=explicit_paths,
            implicit_relations=implicit_rels,
        )

    # ------------------------------------------------------------------
    # Depth 2: Reasoning (multi-hop + implicit, < 200ms)
    # ------------------------------------------------------------------

    async def _reasoning_retrieve(self, understanding: QueryUnderstanding) -> RetrievalResult:
        """Multi-hop path retrieval with implicit relations and bridge entities."""
        all_nodes = []
        explicit_paths = []
        bridge_entities = []

        entity_ids = [e.replace(" ", "_") for e in understanding.entities]

        # Part 1: Multi-hop paths between entity pairs
        if len(entity_ids) >= 2:
            path_records = await self._db.execute_read(
                """
                MATCH path = (a)-[r*1..3]-(b)
                WHERE a.id = $from_id AND b.id = $to_id
                RETURN [node in nodes(path) | {id: node.id, name: node.name, summary: node.summary}] AS path_nodes,
                       [rel in relationships(path) | {
                           from: startNode(rel).name,
                           to: endNode(rel).name,
                           type: CASE WHEN rel.type IS NOT NULL THEN rel.type ELSE type(rel) END,
                           confidence: rel.confidence
                       }] AS path_rels
                LIMIT 5
                """,
                {"from_id": entity_ids[0], "to_id": entity_ids[-1]},
            )
            for r in path_records:
                all_nodes.extend(r.get("path_nodes", []))
                explicit_paths.extend(r.get("path_rels", []))

        # Part 2: Individual node data
        for entity_name in understanding.entities:
            entity_id = entity_name.replace(" ", "_")
            records = await self._db.execute_read(
                "MATCH (n) WHERE n.id = $id OR n.name = $name "
                "RETURN n.id AS id, n.name AS name, n.content AS content, n.summary AS summary",
                {"id": entity_id, "name": entity_name},
            )
            all_nodes.extend(dict(r) for r in records)

        # Part 3: Bridge entities (nodes connecting different clusters)
        if len(entity_ids) >= 2:
            bridge_records = await self._db.execute_read(
                """
                MATCH (a), (b) WHERE a.id = $from_id AND b.id = $to_id
                OPTIONAL MATCH (bridge) WHERE bridge.page_rank > 0.05
                RETURN DISTINCT bridge.name AS name, bridge.summary AS summary
                LIMIT 5
                """,
                {"from_id": entity_ids[0], "to_id": entity_ids[-1]},
            )
            bridge_entities = [dict(r) for r in bridge_records if r.get("name")]

        # Part 4: Implicit relations
        implicit_rels = await self._get_implicit_relations(understanding.entities)

        return RetrievalResult(
            nodes=self._deduplicate_nodes(all_nodes),
            explicit_paths=explicit_paths,
            implicit_relations=implicit_rels,
            bridge_entities=bridge_entities,
        )

    # ------------------------------------------------------------------
    # Depth 3: Global (cluster scan, < 500ms)
    # ------------------------------------------------------------------

    async def _global_retrieve(self, understanding: QueryUnderstanding) -> RetrievalResult:
        """Global scan across clusters with inter-cluster bridging."""
        all_nodes = []
        cluster_info = []

        # Get all nodes with cluster info
        node_records = await self._db.execute_read(
            """
            MATCH (n) WHERE n:Entity OR n:Concept
            RETURN n.id AS id, n.name AS name, n.summary AS summary,
                   n.cluster_id AS cluster_id, n.page_rank AS page_rank
            ORDER BY n.page_rank DESC
            LIMIT 50
            """,
            {},
        )
        all_nodes = [dict(r) for r in node_records]

        # Cluster info
        cluster_records = await self._db.execute_read(
            "MATCH (c:Cluster) RETURN c.id AS id, c.label AS label, "
            "c.summary AS summary, c.node_count AS node_count",
            {},
        )
        cluster_info = [dict(r) for r in cluster_records]

        # All implicit relations
        implicit_records = await self._db.execute_read(
            """
            MATCH (n)-[r:IMPLICIT]->(m)
            RETURN n.name AS from_name, r.type AS rel_type,
                   r.confidence AS confidence, r.evidence AS evidence, m.name AS to_name
            ORDER BY r.confidence DESC
            LIMIT 30
            """,
            {},
        )
        implicit_rels = [dict(r) for r in implicit_records]

        return RetrievalResult(
            nodes=all_nodes,
            implicit_relations=implicit_rels,
            cluster_info=cluster_info,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_implicit_relations(self, entity_names: list[str]) -> list[dict[str, Any]]:
        """Fetch implicit relations for given entities."""
        entity_ids = [e.replace(" ", "_") for e in entity_names]
        records = await self._db.execute_read(
            """
            MATCH (n)-[r:IMPLICIT]->(m)
            WHERE n.id IN $ids OR m.id IN $ids
               OR n.name IN $names OR m.name IN $names
            RETURN n.name AS from_name, r.type AS rel_type,
                   r.confidence AS confidence, r.evidence AS evidence, m.name AS to_name
            ORDER BY r.confidence DESC
            LIMIT 15
            """,
            {"ids": entity_ids, "names": entity_names},
        )
        return [dict(r) for r in records]

    @staticmethod
    def _deduplicate_nodes(nodes: list[dict]) -> list[dict]:
        """Remove duplicate nodes by id."""
        seen: set[str] = set()
        result = []
        for node in nodes:
            nid = node.get("id", "")
            if nid and nid not in seen:
                seen.add(nid)
                result.append(node)
        return result
