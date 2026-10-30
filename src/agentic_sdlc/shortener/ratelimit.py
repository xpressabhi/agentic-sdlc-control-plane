"""In-process token-bucket rate limiting.

A single-process prototype uses memory. Production would use Redis with the same
interface (``allow(key, cost) -> (allowed, retry_after)``) so route code is
unchanged.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated_at: float = field(default_factory=time.monotonic)


class TokenBucketLimiter:
    def __init__(self, capacity: int, refill_per_second: float | None = None) -> None:
        self.capacity = max(1, capacity)
        self.refill_per_second = refill_per_second or max(0.1, capacity / 60.0)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) > 50_000:
                    self._buckets.clear()
                bucket = _Bucket(tokens=float(self.capacity), updated_at=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            missing = cost - bucket.tokens
            return False, missing / self.refill_per_second

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
