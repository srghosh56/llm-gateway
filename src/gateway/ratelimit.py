"""Per-tenant token-bucket rate limiter, held in process memory.

Each tenant has a bucket of ``burst`` tokens that refills at ``rpm / 60`` tokens per second;
a request spends one token. This allows short bursts while enforcing the average rate.

Limitation (documented on purpose): state is per process. With N gateway replicas a tenant
effectively gets N x the limit, and limits reset on restart. Production would move this to
Redis (atomic Lua script) or enforce it at a shared edge proxy.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        # Injectable clock makes refill behaviour testable without sleeping.
        self._clock = clock
        self._buckets: dict[int, _Bucket] = {}

    def acquire(self, key: int, rpm: int, burst: int) -> tuple[bool, float]:
        """Try to spend one token. Returns (allowed, seconds until a token is available).

        No lock needed: this method never awaits, so on a single asyncio event loop it runs
        atomically with respect to other requests.
        """
        now = self._clock()
        rate = rpm / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(tokens=float(burst), updated=now)
        else:
            bucket.tokens = min(float(burst), bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        retry_after = (1.0 - bucket.tokens) / rate if rate > 0 else 60.0
        return False, retry_after
