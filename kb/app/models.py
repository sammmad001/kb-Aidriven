"""Pydantic data models for the entire knowledge base system."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MaterialType(str, Enum):
    """Five material classification types (素材五类分型)."""
    FACTUAL = "factual"
    CONCEPTUAL = "conceptual"
    EXPERIENTIAL = "experiential"
    COMPARATIVE = "comparative"
    RELATIONAL = "relational"


class ConflictType(str, Enum):
    """Conflict detection types."""
    FACTUAL_CONFLICT = "factual_conflict"
    OPINION_CONFLICT = "opinion_conflict"
    TEMPORAL_UPDATE = "temporal_update"


class ImplicitRelationType(str, Enum):
    """Five implicit relation types."""
    DEPENDS_ON = "depends_on"
    TRADE_OFF = "trade_off"
    BRIDGES = "bridges"
    EVOLVES_TO = "evolves_to"
    SOLVES = "solves"


class QueryType(str, Enum):
    """Query classification types."""
    FACTUAL = "factual"
    RELATIONAL = "relational"
    REASONING = "reasoning"
    GLOBAL = "global"


class TaskStatusEnum(str, Enum):
    """Background task status."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class InputFormat(str, Enum):
    """Supported input formats."""
    TEXT = "text"
    MARKDOWN = "markdown"
    URL = "url"
    FILE = "file"
    IMAGE = "image"
    AUDIO = "audio"


# ---------------------------------------------------------------------------
# Ingest Models
# ---------------------------------------------------------------------------

class IngestOptions(BaseModel):
    """Options for an ingest request."""
    format: InputFormat = InputFormat.TEXT
    channel: str = "api"
    tags: list[str] = Field(default_factory=list)
    file_name: Optional[str] = None
    file_mime: Optional[str] = None


class IngestRequest(BaseModel):
    """API request body for knowledge ingestion.

    source max_length accommodates base64-encoded files up to ~20MB
    (base64 inflates by ~33%, so 20MB → ~28M chars; rounded to 30M).
    """
    source: str = Field(..., max_length=30_000_000)
    options: IngestOptions = Field(default_factory=IngestOptions)


class PreprocessResult(BaseModel):
    """Output of Step 1: preprocessing."""
    content: str
    raw_path: str
    title: str
    format: InputFormat
    word_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntityInfo(BaseModel):
    """Information about an extracted entity."""
    name: str
    exists: bool = False
    node_id: Optional[str] = None


class RelationInfo(BaseModel):
    """Information about an extracted relation."""
    from_entity: str
    to_entity: str
    type: str
    evidence: str = ""


class ConflictInfo(BaseModel):
    """Detected conflict between existing and new knowledge."""
    node: str
    field: str = ""
    existing: str = ""
    new: str = ""
    conflict_type: ConflictType = ConflictType.TEMPORAL_UPDATE


class AnalysisReport(BaseModel):
    """Output of Step 2: analysis & classification."""
    type: MaterialType
    entities: list[EntityInfo] = Field(default_factory=list)
    relations: list[RelationInfo] = Field(default_factory=list)
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    compile_suggestion: str = ""


class CompileAction(BaseModel):
    """A single compile action (create/update node or edge)."""
    action: str
    label: str  # Entity, Concept, Comparison, Synthesis
    entity_name: str
    is_new: bool = True
    properties: dict[str, Any] = Field(default_factory=dict)


class ImplicitRelation(BaseModel):
    """An implicit relation discovered by LLM."""
    source: str
    target: str
    type: ImplicitRelationType
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return round(max(0.0, min(1.0, v)), 2)


class GraphProcessResult(BaseModel):
    """Output of Step 3: graph processing."""
    nodes_created: list[str] = Field(default_factory=list)
    nodes_updated: list[str] = Field(default_factory=list)
    explicit_edges: list[str] = Field(default_factory=list)
    implicit_relations: list[ImplicitRelation] = Field(default_factory=list)
    affected_nodes: list[str] = Field(default_factory=list)
    cluster_updates: list[int] = Field(default_factory=list)


class IngestResult(BaseModel):
    """Final result of a complete ingest pipeline run."""
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    status: TaskStatusEnum = TaskStatusEnum.QUEUED
    raw_path: str = ""
    analysis: Optional[AnalysisReport] = None
    graph_result: Optional[GraphProcessResult] = None
    rendered_files: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Query Models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    """API request body for knowledge query."""
    question: str = Field(..., max_length=5000)


class QueryUnderstanding(BaseModel):
    """Output of Query Step 1: understanding."""
    query_type: QueryType
    entities: list[str] = Field(default_factory=list)
    depth: int = Field(ge=0, le=3, default=0)
    keywords: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """Output of Query Step 2: graph retrieval."""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    explicit_paths: list[dict[str, Any]] = Field(default_factory=list)
    implicit_relations: list[dict[str, Any]] = Field(default_factory=list)
    bridge_entities: list[dict[str, Any]] = Field(default_factory=list)
    cluster_info: list[dict[str, Any]] = Field(default_factory=list)


class SourceReference(BaseModel):
    """A reference to a knowledge node used in an answer."""
    node_id: str
    node_name: str
    relevance: float = Field(ge=0.0, le=1.0, default=1.0)


class QueryResult(BaseModel):
    """Final result of a complete query pipeline run."""
    answer: str
    sources: list[SourceReference] = Field(default_factory=list)
    implicit_relations_used: list[ImplicitRelation] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    query_type: QueryType = QueryType.FACTUAL
    depth: int = 0


# ---------------------------------------------------------------------------
# Task Status
# ---------------------------------------------------------------------------

class TaskStatus(BaseModel):
    """Status of a background ingest task."""
    task_id: str
    status: TaskStatusEnum = TaskStatusEnum.QUEUED
    progress: str = "queued"
    result: Optional[IngestResult] = None
    error: Optional[str] = None
    finished_at: Optional[float] = None  # time.time() when completed/failed


# ---------------------------------------------------------------------------
# Graph / Stats Models
# ---------------------------------------------------------------------------

class GraphStats(BaseModel):
    """Statistics about the knowledge graph."""
    node_count: int = 0
    edge_count: int = 0
    cluster_count: int = 0
    entity_count: int = 0
    concept_count: int = 0
    implicit_edge_count: int = 0


class LintReport(BaseModel):
    """Result of a lint quality check."""
    orphan_nodes: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_groups: list[dict[str, Any]] = Field(default_factory=list)
    low_confidence_edges: list[dict[str, Any]] = Field(default_factory=list)
    broken_relations: list[dict[str, Any]] = Field(default_factory=list)
    fixed_count: int = 0


# ---------------------------------------------------------------------------
# Graph Visualization / Node Report Models
# ---------------------------------------------------------------------------

class RelationDetail(BaseModel):
    """A single relationship detail for node report."""
    source_id: str = ""
    source_name: str = ""
    target_id: str = ""
    target_name: str = ""
    rel_type: str = ""
    implicit_type: Optional[str] = None
    confidence: Optional[float] = None
    evidence: Optional[str] = None
    direction: Literal["outgoing", "incoming"] = "outgoing"


class NodeDetail(BaseModel):
    """Full detail of a single graph node."""
    id: str
    name: str
    node_type: str = "Entity"
    summary: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    page_rank: float = 0.0
    cluster_id: int = 0
    in_degree: int = 0
    out_degree: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    relations: list[RelationDetail] = Field(default_factory=list)


class MultiHopPath(BaseModel):
    """A multi-hop path from the source node."""
    target_id: str
    target_name: str
    hop_count: int
    path_nodes: list[str] = Field(default_factory=list)
    path_relations: list[str] = Field(default_factory=list)


class ClusterBrief(BaseModel):
    """Brief info about a cluster."""
    cluster_id: int
    label: str = ""
    node_count: int = 0
    summary: str = ""


class KnowledgeChainReport(BaseModel):
    """Complete knowledge chain report for a node."""
    node: NodeDetail
    direct_relations: list[RelationDetail] = Field(default_factory=list)
    multi_hop_paths: list[MultiHopPath] = Field(default_factory=list)
    implicit_relations: list[RelationDetail] = Field(default_factory=list)
    cluster_info: Optional[ClusterBrief] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
