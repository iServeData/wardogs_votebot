"""In-memory token-bucket rate limiting with abuse strikes.

Buckets are generous for read-only queries (so players can check often) and
tight for actions that touch the game server (vote submission, in-game
link codes). Users who keep hammering after being limited get a temporary
block during which the bot stops answering them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BucketSpec:
    capacity: float
    refill_per_s: float


# name -> spec. Tuned so normal use never notices the limiter.
SPECS: dict[str, BucketSpec] = {
    "query": BucketSpec(capacity=8, refill_per_s=1 / 3),        # burst 8, then 1 every 3 s
    "vote": BucketSpec(capacity=3, refill_per_s=1 / 10),        # burst 3, then 1 every 10 s
    "link": BucketSpec(capacity=2, refill_per_s=1 / 120),       # in-game whisper: 1 every 2 min
    "verify": BucketSpec(capacity=5, refill_per_s=1 / 30),
    "autocomplete": BucketSpec(capacity=40, refill_per_s=4),    # typing generates many requests
    "admin": BucketSpec(capacity=10, refill_per_s=1 / 2),
    "global": BucketSpec(capacity=60, refill_per_s=20),         # whole bot, all users
}

STRIKE_WINDOW_S = 60
STRIKES_TO_BLOCK = 6
BLOCK_SECONDS = 300


class RateLimiter:
    def __init__(self, specs: dict[str, BucketSpec] | None = None):
        self.specs = specs or SPECS
        self._buckets: dict[tuple[str, int], tuple[float, float]] = {}  # -> (tokens, last)
        self._strikes: dict[int, list[float]] = {}
        self._blocked: dict[int, float] = {}
        self._last_gc = time.monotonic()

    def blocked_for(self, user_id: int) -> float:
        until = self._blocked.get(user_id, 0)
        rem = until - time.monotonic()
        if rem <= 0:
            self._blocked.pop(user_id, None)
            return 0.0
        return rem

    def take(self, bucket: str, user_id: int, cost: float = 1.0) -> float:
        """Consume tokens. Returns 0 if allowed, else seconds until allowed."""
        now = time.monotonic()
        self._gc(now)
        spec = self.specs[bucket]
        tokens, last = self._buckets.get((bucket, user_id), (spec.capacity, now))
        tokens = min(spec.capacity, tokens + (now - last) * spec.refill_per_s)
        if tokens >= cost:
            self._buckets[(bucket, user_id)] = (tokens - cost, now)
            return 0.0
        self._buckets[(bucket, user_id)] = (tokens, now)
        if bucket != "global":
            self._strike(user_id, now)
        return (cost - tokens) / spec.refill_per_s

    def _strike(self, user_id: int, now: float) -> None:
        s = [t for t in self._strikes.get(user_id, []) if now - t < STRIKE_WINDOW_S]
        s.append(now)
        self._strikes[user_id] = s
        if len(s) >= STRIKES_TO_BLOCK:
            self._blocked[user_id] = now + BLOCK_SECONDS
            self._strikes[user_id] = []

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        for key, (tokens, last) in list(self._buckets.items()):
            if now - last > 3600:
                del self._buckets[key]
        for uid, s in list(self._strikes.items()):
            if not s or now - s[-1] > STRIKE_WINDOW_S:
                del self._strikes[uid]
