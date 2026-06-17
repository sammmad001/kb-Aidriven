"""V1.2: QualityGate — validates retrieval sufficiency before LLM generation.

Prevents LLM calls with empty or insufficient context, which was a major
source of hallucinated answers (e.g., the "海力士" query failure where
entity extraction produced zero matches but LLM was still called).
"""

from __future__ import annotations

from enum import Enum

from app.models import EntityResolution, RetrievalResult


class GateDecision(Enum):
    """Quality assessment of retrieval results before passing to LLM."""
    SUFFICIENT = "sufficient"       # Enough data → proceed to generation
    PARTIAL = "partial"             # Some data, some entities unresolved → generate + warn
    INSUFFICIENT = "insufficient"   # No usable data → skip LLM, return guidance


class QualityGate:
    """Assesses whether retrieved data is sufficient for LLM answer generation.

    Design principle: data must pass through this gate before any LLM call.
    If the gate returns INSUFFICIENT, the pipeline returns a structured
    "no results" response without wasting an LLM token.
    """

    # Minimum number of nodes/paths required to consider retrieval "sufficient"
    MIN_NODES = 1

    def __init__(self, min_nodes: int = 1) -> None:
        self._min_nodes = max(min_nodes, 1)

    def assess(
        self,
        retrieval: RetrievalResult,
        resolution: EntityResolution | None = None,
    ) -> GateDecision:
        """Evaluate retrieval quality.

        Args:
            retrieval: The result from the graph retrieval step.
            resolution: Optional entity resolution result for partial detection.

        Returns:
            GateDecision.SUFFICIENT if there's enough data for LLM generation.
            GateDecision.PARTIAL if some entities weren't resolved.
            GateDecision.INSUFFICIENT if there's no usable data at all.
        """
        has_nodes = len(retrieval.nodes) >= self._min_nodes
        has_paths = len(retrieval.explicit_paths) > 0

        if not has_nodes and not has_paths:
            return GateDecision.INSUFFICIENT

        if resolution and resolution.unresolved:
            return GateDecision.PARTIAL

        return GateDecision.SUFFICIENT

    def is_passable(self, retrieval: RetrievalResult) -> bool:
        """Quick check: is there enough data to attempt generation?"""
        return bool(retrieval.nodes or retrieval.explicit_paths)
