"""Abstract base class for knowledge source adapters."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedKnowledge:
    """Unified knowledge representation extracted from any source."""

    source_type: str  # "miromind" / "github" / "web" / ...
    source_id: str  # unique identifier within the source (e.g. "session_id:message_id")
    title: str
    content: str  # main knowledge body in Markdown
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                self.content.encode("utf-8")
            ).hexdigest()


class KnowledgeAdapter(ABC):
    """Abstract adapter for transforming raw source data into KB ingest format.

    Each channel (MiroMind, GitHub, Web scraper, etc.) implements this
    interface to plug into the unified ingestion pipeline.
    """

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Unique identifier for this knowledge source (e.g. 'miromind')."""
        ...

    @abstractmethod
    async def extract(self, source_data: dict[str, Any]) -> ExtractedKnowledge:
        """Extract structured knowledge from raw source-specific data."""
        ...

    @abstractmethod
    async def validate(
        self, extracted: ExtractedKnowledge
    ) -> tuple[bool, str]:
        """Validate extracted knowledge meets quality thresholds.

        Returns:
            (is_valid, reason) tuple. reason explains why if not valid.
        """
        ...

    async def transform(
        self, extracted: ExtractedKnowledge
    ) -> tuple[str, dict]:
        """Transform extracted knowledge into KB Ingest API format.

        Returns:
            (source_markdown, IngestOptions) tuple ready for IngestPipeline.
        """
        # Import here to avoid circular dependency
        from app.models import IngestOptions, InputFormat

        tags: list[str] = [f"channel:{self.source_type}"]
        if extracted.metadata:
            tags.append(f"tokens:{extracted.metadata.get('total_tokens', 0)}")

        return extracted.content, IngestOptions(
            format=InputFormat.MARKDOWN,
            channel=self.source_type,
            tags=tags,
        )
