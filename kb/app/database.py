"""Neo4j database connection management and schema initialization."""

from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession

from app.config import Settings

logger = logging.getLogger(__name__)


class Neo4jDatabase:
    """Async Neo4j database wrapper with connection pooling."""

    def __init__(self, settings: Settings) -> None:
        self._uri = settings.neo4j_uri
        self._user = settings.neo4j_user
        self._password = settings.neo4j_password
        self._driver: AsyncDriver | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Whether the database driver is initialized and connected."""
        return self._driver is not None

    async def connect(self) -> None:
        """Create the async driver (connection pool)."""
        self._driver = AsyncGraphDatabase.driver(
            self._uri,
            auth=(self._user, self._password),
            max_connection_pool_size=50,
            connection_acquisition_timeout=10,
        )
        # Verify connectivity
        async with self._driver.session() as session:
            result = await session.run("RETURN 1")
            await result.consume()
        logger.info("Neo4j connected: %s", self._uri)

    async def close(self) -> None:
        """Close the driver and release all connections."""
        if self._driver:
            await self._driver.close()
            self._driver = None
            logger.info("Neo4j connection closed.")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _session(self) -> AsyncSession:
        assert self._driver is not None, "Driver not initialized. Call connect() first."
        return self._driver.session()

    async def execute_write(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a write query and return records as dicts."""
        params = params or {}
        async with self._session() as session:
            result = await session.execute_write(self._run_tx, query, params)
            return result

    async def execute_read(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a read query and return records as dicts."""
        params = params or {}
        async with self._session() as session:
            result = await session.execute_read(self._run_tx, query, params)
            return result

    @staticmethod
    async def _run_tx(tx, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        result = await tx.run(query, params)
        records = await result.data()
        return records

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def initialize_schema(self) -> None:
        """Create indexes and constraints for the knowledge graph."""
        constraints = [
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT concept_id IF NOT EXISTS FOR (n:Concept) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (n:Source) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT cluster_id IF NOT EXISTS FOR (n:Cluster) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT ingest_record_source_id IF NOT EXISTS FOR (n:IngestRecord) REQUIRE n.source_id IS UNIQUE",
        ]
        indexes = [
            "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
            "CREATE INDEX entity_tags IF NOT EXISTS FOR (n:Entity) ON (n.tags)",
            "CREATE INDEX entity_cluster IF NOT EXISTS FOR (n:Entity) ON (n.cluster_id)",
            "CREATE INDEX entity_updated IF NOT EXISTS FOR (n:Entity) ON (n.updated_at)",
            "CREATE INDEX entity_subtype IF NOT EXISTS FOR (n:Entity) ON (n.subtype)",
            "CREATE INDEX entity_domain IF NOT EXISTS FOR (n:Entity) ON (n.domain)",
            "CREATE INDEX concept_name IF NOT EXISTS FOR (n:Concept) ON (n.name)",
            "CREATE INDEX ingest_record_status IF NOT EXISTS FOR (n:IngestRecord) ON (n.status)",
            "CREATE INDEX ingest_record_channel IF NOT EXISTS FOR (n:IngestRecord) ON (n.source_type)",
        ]
        # V1.1: Full-text index for entity alias search
        fulltext_indexes = [
            "CREATE FULLTEXT INDEX entity_alias_ft IF NOT EXISTS FOR (n:Entity|Concept) ON EACH [n.aliases]",
        ]
        async with self._session() as session:
            for stmt in constraints + indexes:
                try:
                    result = await session.run(stmt)
                    await result.consume()
                except Exception as exc:
                    logger.warning("Schema statement skipped: %s (%s)", stmt, exc)
            for stmt in fulltext_indexes:
                try:
                    result = await session.run(stmt)
                    await result.consume()
                except Exception as exc:
                    logger.warning("Fulltext index skipped: %s (%s)", stmt, exc)
        logger.info("Neo4j schema initialized.")

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        """Fetch a single node by its id field."""
        records = await self.execute_read(
            "MATCH (n) WHERE n.id = $id RETURN n",
            {"id": node_id},
        )
        if records:
            node = records[0].get("n")
            return dict(node) if node else None
        return None

    async def get_nodes_summary(self) -> str:
        """Get a brief text summary of all nodes for LLM context."""
        records = await self.execute_read(
            "MATCH (n) WHERE n:Entity OR n:Concept RETURN n.id AS id, n.name AS name, n.summary AS summary LIMIT 200"
        )
        lines = []
        for r in records:
            summary = r.get("summary", "")
            lines.append(f"- {r['name']}: {summary}" if summary else f"- {r['name']}")
        return "\n".join(lines)

    async def search_entities(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fuzzy search entities by name/id/tags."""
        records = await self.execute_read(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND (n.id CONTAINS $kw OR n.name CONTAINS $kw
                   OR ANY(tag IN n.tags WHERE tag CONTAINS $kw))
            RETURN n.id AS id, n.name AS name, n.summary AS summary, labels(n) AS labels
            LIMIT $limit
            """,
            {"kw": keyword, "limit": limit},
        )
        return records

    # ------------------------------------------------------------------
    # V1.1: Three-tier entity matching + alias search
    # ------------------------------------------------------------------

    async def search_by_alias(self, alias: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search entities by alias using full-text index.
        
        Falls back to CONTAINS-based search if fulltext index is unavailable.
        """
        try:
            records = await self.execute_read(
                """
                CALL db.index.fulltext.queryNodes('entity_alias_ft', $alias)
                YIELD node, score
                WHERE score > 0.3
                RETURN node.id AS id, node.name AS name, node.summary AS summary,
                       node.aliases AS aliases, labels(node) AS labels, score
                ORDER BY score DESC
                LIMIT $limit
                """,
                {"alias": alias, "limit": limit},
            )
            if records:
                return [dict(r) for r in records]
        except Exception as exc:
            logger.debug("Fulltext alias search failed (falling back to CONTAINS): %s", exc)

        # Fallback: CONTAINS-based alias search
        records = await self.execute_read(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND ANY(a IN n.aliases WHERE a CONTAINS $kw)
            RETURN n.id AS id, n.name AS name, n.summary AS summary,
                   n.aliases AS aliases, labels(n) AS labels
            LIMIT $limit
            """,
            {"kw": alias, "limit": limit},
        )
        return [dict(r) for r in records]

    async def match_entity_tiered(
        self, name: str, aliases: list[str],
    ) -> dict[str, Any]:
        """V1.1: Three-tier entity matching.
        
        Returns: {"node_id": str|None, "matched_by": "exact"|"alias"|"semantic"|"new",
                   "match_confidence": 1.0|0.9|0.6|0.0}
        """
        # Tier 1: Exact match on id or name
        exact_result = await self.get_node_by_id(name.replace(" ", "_"))
        if exact_result:
            return {"node_id": exact_result.get("id"), "matched_by": "exact", "match_confidence": 1.0}

        exact_by_name = await self.execute_read(
            "MATCH (n) WHERE n.name = $name RETURN n.id AS id LIMIT 1",
            {"name": name},
        )
        if exact_by_name:
            return {"node_id": exact_by_name[0]["id"], "matched_by": "exact", "match_confidence": 1.0}

        # Tier 2: Alias match
        for alias in aliases:
            alias_results = await self.search_by_alias(alias, limit=1)
            if alias_results:
                return {"node_id": alias_results[0]["id"], "matched_by": "alias", "match_confidence": 0.9}

        # Also check if any existing node's name is in our aliases list
        for alias in aliases:
            alias_name_match = await self.execute_read(
                "MATCH (n) WHERE n.name = $name RETURN n.id AS id LIMIT 1",
                {"name": alias},
            )
            if alias_name_match:
                return {"node_id": alias_name_match[0]["id"], "matched_by": "alias", "match_confidence": 0.9}

        # Tier 3: Semantic similarity (CONTAINS-based fuzzy match)
        fuzzy_result = await self.execute_read(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND (n.name CONTAINS $name OR $name CONTAINS n.name)
              AND n.name <> $name
            RETURN n.id AS id, n.name AS name
            LIMIT 1
            """,
            {"name": name},
        )
        if fuzzy_result:
            return {"node_id": fuzzy_result[0]["id"], "matched_by": "semantic", "match_confidence": 0.6}

        return {"node_id": None, "matched_by": "new", "match_confidence": 0.0}
