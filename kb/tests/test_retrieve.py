"""Tests for GraphRetriever: factual, relational, reasoning, global strategies.

Covers CI-05: test_query.py previously had no retrieve.py tests.
"""

from __future__ import annotations

import pytest

from app.models import QueryType, QueryUnderstanding
from app.query.retrieve import GraphRetriever
from tests.conftest import MockNeo4jDatabase


# ======================================================================
# Extended Mock Database for Retrieve Queries
# ======================================================================

class RetrieveMockDB(MockNeo4jDatabase):
    """Mock database that routes retrieve-specific Cypher patterns."""

    def __init__(self) -> None:
        super().__init__()
        self._explicit_rels: list[dict] = []
        self._implicit_rels: list[dict] = []
        self._path_records: list[dict] = []
        self._bridge_records: list[dict] = []
        self._cluster_records: list[dict] = []

    async def execute_read(self, query: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        ql = query.lower()

        # Multi-hop path query (reasoning)
        if "match path" in ql:
            return self._path_records

        # Bridge entities query (reasoning)
        if "bridge" in ql and "page_rank" in ql:
            return self._bridge_records

        # Cluster info query (global)
        if "c:cluster" in ql:
            return self._cluster_records

        # Implicit relations (entity-scoped and global)
        if ":implicit" in ql:
            return self._implicit_rels

        # Explicit relationship batch query (relational)
        if ")-[r]->(" in ql:
            return self._explicit_rels

        # Global node scan (global)
        if "n:entity" in ql or "n:concept" in ql:
            return [
                {
                    "id": n["id"],
                    "name": n.get("name", ""),
                    "summary": n.get("summary", ""),
                    "cluster_id": n.get("cluster_id", ""),
                    "page_rank": n.get("page_rank", 0.0),
                }
                for n in self._nodes.values()
            ]

        # Node batch query (factual / relational / reasoning)
        if "in $ids" in ql or "in $names" in ql:
            ids = params.get("ids", [])
            names = params.get("names", [])
            results: list[dict] = []
            for node in self._nodes.values():
                if node.get("id") in ids or node.get("name") in names:
                    results.append({
                        "id": node["id"],
                        "name": node.get("name", ""),
                        "content": node.get("content", ""),
                        "summary": node.get("summary", ""),
                        "tags": node.get("tags", []),
                    })
            return results

        return await super().execute_read(query, params)


# ======================================================================
# GraphRetriever Strategy Tests
# ======================================================================

class TestGraphRetriever:
    """Test Step 2: graph retrieval with adaptive depth."""

    @staticmethod
    def _make_db(
        nodes: list[dict] | None = None,
        explicit_rels: list[dict] | None = None,
        implicit_rels: list[dict] | None = None,
        path_records: list[dict] | None = None,
        bridge_records: list[dict] | None = None,
        cluster_records: list[dict] | None = None,
    ) -> RetrieveMockDB:
        db = RetrieveMockDB()
        for n in (nodes or []):
            db._nodes[n["id"]] = n
        db._explicit_rels = explicit_rels or []
        db._implicit_rels = implicit_rels or []
        db._path_records = path_records or []
        db._bridge_records = bridge_records or []
        db._cluster_records = cluster_records or []
        return db

    # -- Depth 0: Factual ------------------------------------------------

    @pytest.mark.asyncio
    async def test_factual_single_entity(self):
        """Depth-0 factual returns matching node content."""
        db = self._make_db(nodes=[
            {"id": "RAG", "name": "RAG", "content": "RAG is retrieval-augmented generation",
             "summary": "Retrieval Augmented Generation"},
        ])
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.FACTUAL, entities=["RAG"])
        )
        assert len(result.nodes) == 1
        assert result.nodes[0]["id"] == "RAG"
        assert result.explicit_paths == []
        assert result.implicit_relations == []

    @pytest.mark.asyncio
    async def test_factual_empty_entities(self):
        """No entities -> empty result without querying."""
        db = self._make_db()
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.FACTUAL, entities=[])
        )
        assert result.nodes == []

    @pytest.mark.asyncio
    async def test_factual_multiple_entities(self):
        """Multiple entities -> batched lookup."""
        db = self._make_db(nodes=[
            {"id": "RAG", "name": "RAG", "content": "...", "summary": "..."},
            {"id": "LLM", "name": "LLM", "content": "...", "summary": "..."},
        ])
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.FACTUAL, entities=["RAG", "LLM"])
        )
        ids = {n["id"] for n in result.nodes}
        assert ids == {"RAG", "LLM"}

    @pytest.mark.asyncio
    async def test_factual_entity_with_spaces(self):
        """Entity names with spaces are normalised to underscores for id lookup."""
        db = self._make_db(nodes=[
            {"id": "Knowledge_Graph", "name": "Knowledge Graph",
             "content": "...", "summary": "..."},
        ])
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(
                query_type=QueryType.FACTUAL, entities=["Knowledge Graph"]
            )
        )
        assert len(result.nodes) == 1
        assert result.nodes[0]["id"] == "Knowledge_Graph"

    # -- Depth 1: Relational ---------------------------------------------

    @pytest.mark.asyncio
    async def test_relational_returns_nodes_and_paths(self):
        """Depth-1 relational returns nodes + explicit paths + implicit."""
        db = self._make_db(
            nodes=[
                {"id": "RAG", "name": "RAG", "content": "...", "summary": "..."},
            ],
            explicit_rels=[
                {"from_name": "RAG", "rel_type": "uses", "to_name": "LLM",
                 "to_summary": "Large Language Model", "to_id": "LLM",
                 "confidence": 0.9},
            ],
            implicit_rels=[
                {"from_name": "RAG", "rel_type": "depends_on",
                 "to_name": "知识图谱", "confidence": 0.85, "evidence": "..."},
            ],
        )
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.RELATIONAL, entities=["RAG"], depth=1)
        )
        assert any(n["id"] == "RAG" for n in result.nodes)
        assert len(result.explicit_paths) == 1
        assert result.explicit_paths[0]["from_name"] == "RAG"
        assert len(result.implicit_relations) == 1

    @pytest.mark.asyncio
    async def test_relational_empty_entities(self):
        """No entities -> empty result."""
        db = self._make_db()
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.RELATIONAL, entities=[], depth=1)
        )
        assert result.nodes == []

    # -- Depth 2: Reasoning ----------------------------------------------

    @pytest.mark.asyncio
    async def test_reasoning_multi_hop_paths(self):
        """Depth-2 reasoning with >=2 entities returns multi-hop paths and bridges."""
        db = self._make_db(
            nodes=[
                {"id": "RAG", "name": "RAG", "content": "...", "summary": "..."},
                {"id": "GraphRAG", "name": "GraphRAG", "content": "...", "summary": "..."},
            ],
            path_records=[
                {"path_nodes": [
                    {"id": "RAG", "name": "RAG", "summary": "..."},
                    {"id": "Bridge", "name": "Bridge", "summary": "桥接实体"},
                    {"id": "GraphRAG", "name": "GraphRAG", "summary": "..."},
                ],
                 "path_rels": [
                    {"from": "RAG", "to": "Bridge", "type": "evolves_to", "confidence": 0.8},
                    {"from": "Bridge", "to": "GraphRAG", "type": "bridges", "confidence": 0.7},
                ]},
            ],
            bridge_records=[
                {"name": "知识图谱", "summary": "Knowledge Graph"},
            ],
            implicit_rels=[
                {"from_name": "RAG", "rel_type": "evolves_to", "to_name": "GraphRAG",
                 "confidence": 0.9, "evidence": "..."},
            ],
        )
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(
                query_type=QueryType.REASONING, entities=["RAG", "GraphRAG"], depth=2,
            )
        )
        # Nodes from path + batch
        assert len(result.nodes) >= 2
        # Explicit paths from multi-hop
        assert len(result.explicit_paths) == 2
        # Bridge entities
        assert len(result.bridge_entities) == 1
        assert result.bridge_entities[0]["name"] == "知识图谱"
        # Implicit relations
        assert len(result.implicit_relations) == 1

    @pytest.mark.asyncio
    async def test_reasoning_single_entity(self):
        """Single entity -> no path or bridge queries (skips multi-hop section)."""
        db = self._make_db(nodes=[
            {"id": "RAG", "name": "RAG", "content": "...", "summary": "..."},
        ])
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.REASONING, entities=["RAG"], depth=2)
        )
        assert len(result.nodes) >= 1
        assert result.explicit_paths == []
        assert result.bridge_entities == []

    # -- Depth 3: Global --------------------------------------------------

    @pytest.mark.asyncio
    async def test_global_cluster_scan(self):
        """Depth-3 global returns all nodes, clusters, implicit relations."""
        db = self._make_db(
            nodes=[
                {"id": "RAG", "name": "RAG", "summary": "...", "page_rank": 0.9},
                {"id": "LLM", "name": "LLM", "summary": "...", "page_rank": 0.8},
            ],
            implicit_rels=[
                {"from_name": "RAG", "rel_type": "depends_on",
                 "to_name": "知识图谱", "confidence": 0.9, "evidence": "..."},
            ],
            cluster_records=[
                {"id": "c1", "label": "AI技术", "summary": "...", "node_count": 5},
            ],
        )
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.GLOBAL, entities=[], depth=3)
        )
        assert len(result.nodes) == 2
        assert len(result.cluster_info) == 1
        assert result.cluster_info[0]["label"] == "AI技术"
        assert len(result.implicit_relations) == 1

    @pytest.mark.asyncio
    async def test_global_empty_db(self):
        """Empty database -> empty result without errors."""
        db = self._make_db()
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.GLOBAL, entities=[], depth=3)
        )
        assert result.nodes == []
        assert result.cluster_info == []
        assert result.implicit_relations == []

    # -- Dispatch ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_dispatch_routes_by_type(self):
        """Factual dispatch does not populate cluster_info; global does."""
        # Factual -> depth-0 strategy
        db = self._make_db(nodes=[
            {"id": "RAG", "name": "RAG", "content": "...", "summary": "..."},
        ])
        retriever = GraphRetriever(db)
        result = await retriever.retrieve(
            QueryUnderstanding(query_type=QueryType.FACTUAL, entities=["RAG"])
        )
        assert result.bridge_entities == []
        assert result.cluster_info == []

        # Global -> depth-3 strategy
        db2 = self._make_db(
            nodes=[{"id": "RAG", "name": "RAG", "summary": "..."}],
            cluster_records=[{"id": "c1", "label": "X", "summary": "", "node_count": 1}],
        )
        retriever2 = GraphRetriever(db2)
        result2 = await retriever2.retrieve(
            QueryUnderstanding(query_type=QueryType.GLOBAL, entities=[], depth=3)
        )
        assert len(result2.cluster_info) == 1


# ======================================================================
# Deduplication Helper Tests
# ======================================================================

class TestDeduplicateNodes:
    """Test the _deduplicate_nodes static helper."""

    def test_removes_duplicate_ids(self):
        nodes = [{"id": "A"}, {"id": "B"}, {"id": "A"}]
        assert len(GraphRetriever._deduplicate_nodes(nodes)) == 2

    def test_preserves_first_occurrence(self):
        nodes = [{"id": "A", "v": 1}, {"id": "A", "v": 2}]
        result = GraphRetriever._deduplicate_nodes(nodes)
        assert result[0]["v"] == 1

    def test_empty_id_filtered_out(self):
        """Nodes without id are filtered out (not included in result)."""
        nodes = [{"id": "", "n": 1}, {"id": "", "n": 2}, {"id": "X"}]
        result = GraphRetriever._deduplicate_nodes(nodes)
        assert len(result) == 1
        assert result[0]["id"] == "X"

    def test_empty_list(self):
        assert GraphRetriever._deduplicate_nodes([]) == []
