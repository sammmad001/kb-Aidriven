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
            "CREATE INDEX concept_name IF NOT EXISTS FOR (n:Concept) ON (n.name)",
            "CREATE INDEX ingest_record_status IF NOT EXISTS FOR (n:IngestRecord) ON (n.status)",
            "CREATE INDEX ingest_record_channel IF NOT EXISTS FOR (n:IngestRecord) ON (n.source_type)",
        ]
        async with self._session() as session:
            for stmt in constraints + indexes:
                try:
                    result = await session.run(stmt)
                    await result.consume()
                except Exception as exc:
                    logger.warning("Schema statement skipped: %s (%s)", stmt, exc)
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
