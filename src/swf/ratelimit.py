"""In-process token-bucket rate limiter (m8 §1.2). One API process only (§0.7)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable


class RateLimiter:
    """Token bucket per key; thread-safe; in-process (one API process only, §0.7)."""

    def __init__(
        self,
        *,
        per_minute: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if per_minute <= 0 or burst < 1 or max_keys < 1:
            raise ValueError("per_minute > 0, burst >= 1 and max_keys >= 1 required")
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._max = max_keys
        self._buckets: dict[Hashable, tuple[float, float]] = {}  # key -> (tokens, at)
        self._lock = threading.Lock()

    def hit(self, key: Hashable) -> float | None:
        """None = allowed (one token used); else seconds to wait. A refused hit uses no token."""
        with self._lock:
            now = self._clock()
            tokens, at = self._buckets.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - at) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._prune(now)
                return None
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self._rate

    def _prune(self, now: float) -> None:
        """Beyond max_keys, drop keys whose bucket has refilled to full."""
        if len(self._buckets) <= self._max:
            return
        full = [
            k for k, (t, at) in self._buckets.items() if t + (now - at) * self._rate >= self._burst
        ]
        for k in full:
            del self._buckets[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
