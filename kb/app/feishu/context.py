"""Conversation context: per-user query history with TTL expiry.

Stores recent Q&A turns in memory so follow-up questions can be enriched
with entity context (e.g. "它的优点" → "RAG的优点").

Uses the same OrderedDict + TTL pattern as _MessageDeduplicator in handlers.py.
Isolated by user_id (Feishu open_id → kb user_id).
"""

from __future__ import annotations

import itertools
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Follow-up reference patterns — when matched, entity enrichment is attempted
_REFERENCE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(它的|这个|那个|该|此|前面提到的|刚才说的)"),
    re.compile(r"(它|这|那)(有|是|能|会|可以)"),
]

# Entity extraction from answer: capitalized words or CJK terms in bold/markdown
_ENTITY_PATTERN = re.compile(r"\*\*(.+?)\*\*")


@dataclass
class ConversationTurn:
    """A single Q&A turn in conversation history."""
    question: str
    answer: str
    entities: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


class ConversationContext:
    """Per-user conversation context with TTL expiry.

    Thread-safe for asyncio (single-threaded event loop, no locks needed).
    """

    def __init__(
        self,
        max_turns: int = 5,
        ttl_seconds: int = 600,
    ) -> None:
        self._max_turns = max_turns
        self._ttl = ttl_seconds
        # user_id → OrderedDict[turn_key, ConversationTurn]
        self._contexts: dict[str, OrderedDict[str, ConversationTurn]] = {}
        # Monotonic counter ensures unique keys even when turns are added in rapid succession
        self._counter = itertools.count()

    def add_turn(
        self,
        user_id: str,
        question: str,
        answer: str,
        entities: list[str] | None = None,
    ) -> None:
        """Record a Q&A turn for the user."""
        ctx = self._contexts.setdefault(user_id, OrderedDict())

        # Extract entities from answer if not provided
        if entities is None:
            entities = self._extract_entities(answer)

        turn_key = f"{time.time():.3f}_{next(self._counter)}"
        turn = ConversationTurn(
            question=question,
            answer=answer,
            entities=entities,
        )

        ctx[turn_key] = turn
        # Evict oldest if over capacity
        while len(ctx) > self._max_turns:
            ctx.popitem(last=False)

        logger.debug(
            "Context turn added: user=%s turns=%d entities=%s",
            user_id[:8], len(ctx), entities[:3],
        )

    def get_history(self, user_id: str) -> list[ConversationTurn]:
        """Get conversation history for a user (within TTL window)."""
        self._evict_expired(user_id)
        ctx = self._contexts.get(user_id)
        if not ctx:
            return []
        return list(ctx.values())

    def has_active_context(self, user_id: str) -> bool:
        """Check if the user has recent conversation context."""
        self._evict_expired(user_id)
        ctx = self._contexts.get(user_id)
        return bool(ctx and len(ctx) > 0)

    def enrich_followup(self, user_id: str, text: str) -> str:
        """Enrich a follow-up question with entity context.

        Example: "它的优点" → "RAG的优点" (if last turn was about RAG).

        Returns the original text if no enrichment is needed or possible.
        """
        history = self.get_history(user_id)
        if not history:
            return text

        # Check if text contains reference words
        has_reference = any(p.search(text) for p in _REFERENCE_PATTERNS)
        if not has_reference:
            return text

        # Get the most recent turn's entities
        last_turn = history[-1]
        if not last_turn.entities:
            return text

        # Simple enrichment: prepend the primary entity if text starts with a reference
        primary_entity = last_turn.entities[0]
        enriched = re.sub(
            r"^(它的|它的|这个|那个|该|此)",
            f"{primary_entity}的",
            text,
            count=1,
        )
        if enriched != text:
            logger.debug(
                "Followup enriched: '%s' → '%s' (entity=%s)",
                text, enriched, primary_entity,
            )
        return enriched

    def get_context_history(self, user_id: str) -> list[dict[str, Any]]:
        """Get conversation history formatted for QueryRequest.context_history.

        Returns interleaved user/assistant messages in chronological order:
        [user:Q1, assistant:A1, user:Q2, assistant:A2, ...]
        """
        history = self.get_history(user_id)
        result: list[dict[str, Any]] = []
        for t in history:
            result.append({"role": "user", "content": t.question})
            result.append({"role": "assistant", "content": t.answer[:500]})
        return result

    def clear(self, user_id: str) -> None:
        """Clear conversation context for a user."""
        self._contexts.pop(user_id, None)

    def _evict_expired(self, user_id: str) -> None:
        """Remove expired turns for a user."""
        ctx = self._contexts.get(user_id)
        if not ctx:
            return
        now = time.time()
        # Remove from front while expired
        while ctx:
            oldest_key = next(iter(ctx))
            if ctx[oldest_key].timestamp < now - self._ttl:
                ctx.popitem(last=False)
            else:
                break
        # Clean up empty context dict
        if not ctx:
            self._contexts.pop(user_id, None)

    @staticmethod
    def _extract_entities(answer: str) -> list[str]:
        """Extract entity names from an answer string.

        Looks for **bold** markdown entities in the answer text.
        """
        matches = _ENTITY_PATTERN.findall(answer)
        # Deduplicate while preserving order
        seen: set[str] = set()
        entities: list[str] = []
        for m in matches:
            name = m.strip()
            if name and name not in seen and len(name) < 50:
                seen.add(name)
                entities.append(name)
        return entities[:10]  # cap at 10 entities
