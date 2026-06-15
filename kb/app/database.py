"""Neo4j database connection management and schema initialization."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession

from app.config import Settings

logger = logging.getLogger(__name__)

# ContextVar for per-request user isolation
_current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")


class Neo4jDatabase:
    """Async Neo4j database wrapper with connection pooling."""

    def __init__(self, settings: Settings) -> None:
        self._uri = settings.neo4j_uri
        self._user = settings.neo4j_user
        self._password = settings.neo4j_password
        self._driver: AsyncDriver | None = None

    # ------------------------------------------------------------------
    # User context (ContextVar-based isolation)
    # ------------------------------------------------------------------

    @staticmethod
    def set_current_user(user_id: str) -> None:
        """Set the current user_id for this async context."""
        _current_user_id.set(user_id)

    @staticmethod
    def get_current_user_id() -> str:
        """Get the current user_id from context. Returns default if unset."""
        uid = _current_user_id.get("")
        if not uid:
            raise RuntimeError("No user_id in context — authentication required")
        return uid

    @staticmethod
    def get_current_user_id_or_default() -> str:
        """Get current user_id or empty string (for background tasks)."""
        return _current_user_id.get("")

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
    # User-aware query helpers (auto-inject user_id from ContextVar)
    # ------------------------------------------------------------------

    async def execute_read_for_user(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a read query with _user_id auto-injected from ContextVar."""
        params = params or {}
        params["_user_id"] = self.get_current_user_id()
        return await self.execute_read(query, params)

    async def execute_write_for_user(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a write query with _user_id auto-injected from ContextVar."""
        params = params or {}
        params["_user_id"] = self.get_current_user_id()
        return await self.execute_write(query, params)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def initialize_schema(self) -> None:
        """Create indexes and constraints for the knowledge graph."""
        # Multi-user: composite uniqueness constraints for data isolation
        # Drop old single-property constraints first (best-effort)
        drop_old_constraints = [
            "DROP CONSTRAINT entity_id IF EXISTS",
            "DROP CONSTRAINT concept_id IF EXISTS",
        ]
        constraints = [
            "CREATE CONSTRAINT entity_id_user IF NOT EXISTS FOR (n:Entity) REQUIRE (n.id, n.user_id) IS UNIQUE",
            "CREATE CONSTRAINT concept_id_user IF NOT EXISTS FOR (n:Concept) REQUIRE (n.id, n.user_id) IS UNIQUE",
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
        # Multi-user: user_id indexes for data isolation
        user_id_indexes = [
            "CREATE INDEX entity_user_id IF NOT EXISTS FOR (n:Entity) ON (n.user_id)",
            "CREATE INDEX concept_user_id IF NOT EXISTS FOR (n:Concept) ON (n.user_id)",
            "CREATE INDEX source_user_id IF NOT EXISTS FOR (n:Source) ON (n.user_id)",
            "CREATE INDEX cluster_user_id IF NOT EXISTS FOR (n:Cluster) ON (n.user_id)",
            "CREATE INDEX ingest_record_user_id IF NOT EXISTS FOR (n:IngestRecord) ON (n.user_id)",
        ]
        async with self._session() as session:
            for stmt in drop_old_constraints:
                try:
                    result = await session.run(stmt)
                    await result.consume()
                except Exception:
                    pass  # Ignore if constraint doesn't exist
            for stmt in constraints + indexes + user_id_indexes:
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
        """Fetch a single node by its id field (auto user_id filtered)."""
        records = await self.execute_read_for_user(
            "MATCH (n) WHERE n.id = $id AND n.user_id = $_user_id RETURN n",
            {"id": node_id},
        )
        if records:
            node = records[0].get("n")
            return dict(node) if node else None
        return None

    async def get_nodes_summary(self) -> str:
        """Get a brief text summary of all nodes for LLM context (user-scoped)."""
        records = await self.execute_read_for_user(
            "MATCH (n) WHERE (n:Entity OR n:Concept) AND n.user_id = $_user_id "
            "RETURN n.id AS id, n.name AS name, n.summary AS summary LIMIT 200"
        )
        lines = []
        for r in records:
            summary = r.get("summary", "")
            lines.append(f"- {r['name']}: {summary}" if summary else f"- {r['name']}")
        return "\n".join(lines)

    async def search_entities(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fuzzy search entities by name/id/tags (user-scoped)."""
        records = await self.execute_read_for_user(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND n.user_id = $_user_id
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
        """Search entities by alias using full-text index (user-scoped).
        
        Falls back to CONTAINS-based search if fulltext index is unavailable.
        """
        try:
            records = await self.execute_read_for_user(
                """
                CALL db.index.fulltext.queryNodes('entity_alias_ft', $alias)
                YIELD node, score
                WHERE score > 0.3 AND node.user_id = $_user_id
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
        records = await self.execute_read_for_user(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND n.user_id = $_user_id
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
        """V1.1: Three-tier entity matching (user-scoped).
        
        Returns: {"node_id": str|None, "matched_by": "exact"|"alias"|"semantic"|"new",
                   "match_confidence": 1.0|0.9|0.6|0.0}
        """
        # Tier 1: Exact match on id or name (namespaced with user_id)
        namespaced_id = f"{self.get_current_user_id()}:{name.replace(' ', '_')}"
        exact_result = await self.get_node_by_id(namespaced_id)
        if exact_result:
            return {"node_id": exact_result.get("id"), "matched_by": "exact", "match_confidence": 1.0}

        # Also try old-style ID for backward compat during migration
        old_id = name.replace(" ", "_")
        exact_result_old = await self.get_node_by_id(old_id)
        if exact_result_old:
            return {"node_id": exact_result_old.get("id"), "matched_by": "exact", "match_confidence": 1.0}

        exact_by_name = await self.execute_read_for_user(
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
            alias_name_match = await self.execute_read_for_user(
                "MATCH (n) WHERE n.name = $name RETURN n.id AS id LIMIT 1",
                {"name": alias},
            )
            if alias_name_match:
                return {"node_id": alias_name_match[0]["id"], "matched_by": "alias", "match_confidence": 0.9}

        # Tier 3: Semantic similarity (CONTAINS-based fuzzy match)
        fuzzy_result = await self.execute_read_for_user(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Concept)
              AND n.user_id = $_user_id
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
