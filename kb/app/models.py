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


class EdgeLabel(str, Enum):
    """V1.1: Extended edge labels for explicit relations."""
    # Original
    EXPLICIT = "EXPLICIT"
    IMPLICIT = "IMPLICIT"
    BELONGS_TO = "BELONGS_TO"
    DERIVED_FROM = "DERIVED_FROM"
    # New: 5 semantic edge types
    CAUSES = "CAUSES"
    PRECEDES = "PRECEDES"
    IS_A = "IS_A"
    CONTRADICTS = "CONTRADICTS"
    ANALOGOUS_TO = "ANALOGOUS_TO"


class ImplicitRelationType(str, Enum):
    """V1.1: Extended from 5 to 9 implicit relation types."""
    # Original 5
    DEPENDS_ON = "depends_on"
    TRADE_OFF = "trade_off"
    BRIDGES = "bridges"
    EVOLVES_TO = "evolves_to"
    SOLVES = "solves"
    # New 4
    PRECEDES = "precedes"        # 时序先后关系
    CAUSES = "causes"            # 因果关系
    CONTRADICTS = "contradicts"   # 矛盾/对立观点
    ANALOGOUS_TO = "analogous_to" # 类比关系


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


class SocialPlatform(str, Enum):
    """Supported social media platforms for content fetching."""
    XIAOHONGSHU = "xiaohongshu"
    WEIBO = "weibo"


class FetchStatus(str, Enum):
    """Status of a social content fetch operation."""
    FETCHING = "fetching"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Social Media Models
# ---------------------------------------------------------------------------

class SocialImage(BaseModel):
    """A single image extracted from a social media post."""
    url: str
    base64: str = ""         # Downloaded image as base64
    ocr_text: str = ""       # OCR-extracted text from this image
    ocr_engine: str = ""     # Which engine produced the OCR (paddle / qwen-vl)
    width: int = 0
    height: int = 0


class SocialContent(BaseModel):
    """Content extracted from a social media post."""
    url: str
    platform: SocialPlatform
    title: str = ""
    text: str = ""               # Plain text body (paragraph structure preserved)
    tags: list[str] = Field(default_factory=list)  # #话题#
    images: list[SocialImage] = Field(default_factory=list)
    interaction: dict[str, int] = Field(default_factory=dict)  # likes, collects, comments
    author_name: str = ""
    publish_time: str = ""       # ISO datetime string
    fetch_status: FetchStatus = FetchStatus.FETCHING
    error: str = ""

    def to_ingest_markdown(self) -> str:
        """Convert this social content to a Markdown string ready for the IngestPipeline.

        The generated Markdown includes the title, body text, OCR-extracted image text,
        and metadata footer. This is the canonical format fed into Step 1 (Preprocess).
        """
        lines: list[str] = []

        # Title
        if self.title:
            lines.append(f"# {self.title}")
            lines.append("")
        else:
            # Fallback title from URL path
            slug = self.url.rstrip("/").split("/")[-1] or "untitled"
            lines.append(f"# {slug}")
            lines.append("")

        # Source attribution
        platform_cn = "小红书" if self.platform == SocialPlatform.XIAOHONGSHU else "微博"
        lines.append(f"> 来源: [{platform_cn}]({self.url})")
        if self.author_name:
            lines.append(f"> 作者: {self.author_name}")
        if self.publish_time:
            lines.append(f"> 发布时间: {self.publish_time}")
        lines.append("")

        # Body text
        if self.text:
            lines.append(self.text)
            lines.append("")

        # Images with OCR text
        for i, img in enumerate(self.images):
            if img.ocr_text:
                lines.append(f"> 📷 图片 {i+1} 文字提取 (via {img.ocr_engine}):")
                lines.append("> ")
                for ocr_line in img.ocr_text.strip().split("\n"):
                    lines.append(f"> {ocr_line}")
                lines.append("")

        # Tags
        if self.tags:
            lines.append("**话题标签**: " + ", ".join(f"#{t}" for t in self.tags))
            lines.append("")

        # Metadata footer
        lines.append("---")
        lines.append(f"*平台: {platform_cn} | URL: {self.url}*")
        if self.interaction:
            parts = []
            for k, v in self.interaction.items():
                parts.append(f"{k}: {v}")
            lines.append(f"*互动: {' | '.join(parts)}*")

        return "\n".join(lines)


class OCRResult(BaseModel):
    """Result from a single OCR extraction."""
    text: str                 # Extracted text
    engine: str               # "paddle" or "qwen-vl"
    confidence: float = 0.0   # 0.0 - 1.0
    duration_ms: float = 0.0  # Processing time in milliseconds


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
    """V1.1: Information about an extracted entity with alias support and existence cross-validation."""
    name: str
    exists: bool = False
    node_id: Optional[str] = None
    # V1.1: Entity enrichment fields
    aliases: list[str] = Field(default_factory=list)
    subtype: str = ""
    domain: str = ""
    definition: str = ""
    importance: int = Field(default=5, ge=1, le=10)
    # V1.1: Existence cross-validation fields
    exists_guess: bool = False  # LLM's pre-judgment of entity existence
    exists_reason: str = ""     # LLM's reasoning for existence guess
    matched_by: str = ""        # How entity was matched: "exact"|"alias"|"semantic"|"new"


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
    type_confidence: float = Field(default=0.5, ge=0.0, le=1.0)  # V1.1: 分类置信度
    classification_reason: str = ""  # V1.1: LLM 分类判定理由
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


class TokenUsage(BaseModel):
    """LLM token usage statistics from a single API call or accumulated calls."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        """Accumulate another TokenUsage into this one (mutates in-place)."""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


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
    token_usage: Optional["TokenUsage"] = None


# ---------------------------------------------------------------------------
# Query Models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    """API request body for knowledge query."""
    question: str = Field(..., max_length=5000)
    context_history: list[dict[str, Any]] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# User Auth Models
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    """Registration request body."""
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\-]+$")
    password: str = Field(..., min_length=6, max_length=100)


class UserLogin(BaseModel):
    """Login request body."""
    username: str
    password: str


class TokenResponse(BaseModel):
    """JWT token response for login/refresh."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900  # Access token validity in seconds


class RefreshRequest(BaseModel):
    """Refresh token request body."""
    refresh_token: str


class CurrentUser(BaseModel):
    """Authenticated user info (passed through dependency injection)."""
    id: str          # Internal user ID (e.g. "usr_abc123")
    username: str    # Display username
    is_service: bool = False  # True for service account (knowledge_api_token)


class KnowledgeChainReport(BaseModel):
    """Complete knowledge chain report for a node."""
    node: NodeDetail
    direct_relations: list[RelationDetail] = Field(default_factory=list)
    multi_hop_paths: list[MultiHopPath] = Field(default_factory=list)
    implicit_relations: list[RelationDetail] = Field(default_factory=list)
    cluster_info: Optional[ClusterBrief] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
