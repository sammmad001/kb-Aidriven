"""Test fixtures: mock LLM, mock Neo4j, sample data."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.llm import LLMClient
from app.models import (
    AnalysisReport,
    EntityInfo,
    GraphProcessResult,
    ImplicitRelation,
    ImplicitRelationType,
    IngestResult,
    MaterialType,
    QueryRequest,
    QueryResult,
    QueryType,
    RelationInfo,
    SourceReference,
    TaskStatusEnum,
)


# ======================================================================
# Mock LLM Client
# ======================================================================

class MockLLMClient(LLMClient):
    """Mock LLM that returns predetermined responses."""

    def __init__(self, responses: dict[str, str] | None = None):
        self._responses = responses or {}
        self._default_analysis = json.dumps({
            "type": "factual",
            "entities": [{"name": "RAG", "exists": False}],
            "relations": [],
            "conflicts": [],
            "gaps": [],
            "compile_suggestion": "创建RAG实体节点",
        })
        self._default_content = "> RAG是检索增强生成技术\n\nRAG (Retrieval Augmented Generation) 是一种..."
        self._default_implicit = json.dumps([])
        self._default_answer = "RAG（检索增强生成）是一种将检索系统与生成模型结合的技术。"
        self.call_count = 0

    async def chat(self, system: str, user: str, json_mode: bool = False, model: str | None = None) -> str:
        self.call_count += 1
        # Route based on system prompt content
        if "分析器" in system or "分析" in system:
            return self._responses.get("analysis", self._default_analysis)
        if "编译器" in system:
            return self._responses.get("content", self._default_content)
        if "隐式" in system:
            return self._responses.get("implicit", self._default_implicit)
        if "查询分析" in system:
            return self._responses.get("classify", '{"type": "factual", "entities": [], "depth": 0}')
        # Default answer generation
        return self._responses.get("answer", self._default_answer)


# ======================================================================
# Mock Neo4j Database
# ======================================================================

class MockNeo4jDatabase:
    """Mock Neo4j database for testing."""

    def __init__(self):
        self._nodes: dict[str, dict] = {}
        self._edges: list[dict] = []
        self._driver = MagicMock()

    async def connect(self):
        pass

    async def close(self):
        pass

    async def initialize_schema(self):
        pass

    async def execute_write(self, query: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        # Parse simple MERGE operations
        if "MERGE" in query and "Entity" in query:
            node_id = params.get("id", params.get("name", ""))
            self._nodes[node_id] = {
                "id": node_id,
                "name": params.get("name", node_id),
                "content": params.get("content", ""),
                "summary": params.get("summary", ""),
            }
            return [{"id": node_id}]
        if "EXPLICIT" in query:
            edge = {
                "from": params.get("from_id", ""),
                "to": params.get("to_id", ""),
                "type": params.get("edge_type", "related_to"),
            }
            self._edges.append(edge)
            return [{"rel_type": edge["type"]}]
        if "IMPLICIT" in query:
            edge = {
                "from": params.get("from_id", ""),
                "to": params.get("to_id", ""),
                "type": params.get("edge_type", "depends_on"),
                "confidence": params.get("confidence", 0.8),
            }
            self._edges.append(edge)
            return []
        return []

    async def execute_read(self, query: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        if "EXPLICIT" in query and "n.id = $id" in query:
            return []  # No explicit rels in mock
        if "IMPLICIT" in query and "n.id = $id" in query:
            return []  # No implicit rels in mock
        if "m)-[r]->(n)" in query:
            return []  # No incoming rels in mock
        if "n.id = $id" in query or "n.name = $name" in query:
            node_id = params.get("id", params.get("name", ""))
            if node_id in self._nodes:
                return [self._nodes[node_id]]
            # Check by name
            for n in self._nodes.values():
                if n.get("name") == node_id or n.get("name") == params.get("name"):
                    return [n]
            return []
        if "n.name CONTAINS" in query:
            kw = params.get("kw", params.get("name", ""))
            results = []
            for n in self._nodes.values():
                if kw.lower() in n.get("name", "").lower() or kw.lower() in n.get("id", "").lower():
                    results.append({"id": n["id"], "name": n["name"], "summary": n.get("summary", "")})
            return results[:params.get("limit", 10)]
        if "count" in query.lower():
            return [{"node_count": len(self._nodes), "edge_count": len(self._edges),
                     "implicit_count": sum(1 for e in self._edges if "IMPLICIT" in str(e)),
                     "entity_count": len(self._nodes), "concept_count": 0, "cluster_count": 0}]
        if "n.updated_at" in query:
            return []
        return []

    async def get_node_by_id(self, node_id: str) -> dict | None:
        return self._nodes.get(node_id)

    async def get_nodes_summary(self) -> str:
        return "\n".join(f"- {n['name']}: {n.get('summary', '')}" for n in self._nodes.values())

    async def search_entities(self, keyword: str, limit: int = 10) -> list[dict]:
        return await self.execute_read(
            "MATCH (n) WHERE n.name CONTAINS $kw RETURN n.id, n.name LIMIT $limit",
            {"kw": keyword, "limit": limit},
        )


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def settings():
    return Settings(
        neo4j_password="test",
        llm_provider="ollama",
        raw_dir="raw/sources",
        wiki_dir="wiki",
    )


@pytest.fixture
def mock_llm():
    return MockLLMClient()


@pytest.fixture
def mock_db():
    return MockNeo4jDatabase()


@pytest.fixture
def sample_analysis():
    return AnalysisReport(
        type=MaterialType.FACTUAL,
        entities=[EntityInfo(name="RAG", exists=False)],
        relations=[RelationInfo(from_entity="RAG", to_entity="LLM", type="uses", evidence="RAG使用LLM")],
        conflicts=[],
        gaps=[],
        compile_suggestion="创建RAG实体节点",
    )


@pytest.fixture
def sample_ingest_result():
    return IngestResult(
        task_id="test-task-001",
        status=TaskStatusEnum.COMPLETED,
        raw_path="raw/sources/2024-01-15-rag.md",
        graph_result=GraphProcessResult(
            nodes_created=["RAG"],
            nodes_updated=[],
            explicit_edges=["RAG-[uses]->LLM"],
            implicit_relations=[ImplicitRelation(
                source="RAG", target="知识图谱",
                type=ImplicitRelationType.DEPENDS_ON,
                confidence=0.85, evidence="RAG的检索能力依赖知识图谱",
            )],
            affected_nodes=["RAG", "LLM"],
        ),
        rendered_files=["wiki/entities/RAG.md"],
    )
