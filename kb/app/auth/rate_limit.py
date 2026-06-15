"""Per-user rate limiter using token bucket algorithm."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

# Unified rate limits (all users equal, no tier differentiation)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "ingest": (10, 60),   # 10 requests per 60 seconds
    "query":  (30, 60),   # 30 requests per 60 seconds
}


class UserRateLimiter:
    """Per-user token bucket rate limiter (in-memory, single-process safe)."""

    def __init__(self) -> None:
        # {user_id: {action: {"tokens": float, "last_refill": float}}}
        self._buckets: dict[str, dict[str, dict]] = defaultdict(dict)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, user_id: str, action: str) -> bool:
        """Try to consume one token. Returns True if allowed, False if rate-limited."""
        max_tokens, window = RATE_LIMITS.get(action, (5, 60))
        key = f"{user_id}:{action}"

        async with self._locks[key]:
            bucket = self._buckets[user_id].get(action)
            now = time.monotonic()

            if bucket is None:
                # First request: start with full bucket
                bucket = {"tokens": float(max_tokens), "last_refill": now}
                self._buckets[user_id][action] = bucket

            # Refill tokens based on elapsed time
            elapsed = now - bucket["last_refill"]
            refill_rate = max_tokens / window
            bucket["tokens"] = min(float(max_tokens), bucket["tokens"] + elapsed * refill_rate)
            bucket["last_refill"] = now

            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                return True
            return False

    def clear_user(self, user_id: str) -> None:
        """Remove all rate limit state for a user (for testing)."""
        if user_id in self._buckets:
            del self._buckets[user_id]
