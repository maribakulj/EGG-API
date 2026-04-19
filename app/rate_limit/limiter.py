"""In-memory sliding-window rate limiter.

The store is a per-subject deque of timestamps. ``allow(subject)`` evicts any
timestamp older than ``window_seconds`` and accepts the request if fewer than
``max_requests`` remain. This is the simplest shape that matches the public
and admin-login policies: the class is intentionally synchronous, threadsafe
in CPython thanks to GIL-protected deque mutations, and is recreated by
``Container`` whenever the configured limits change.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

# Conservative defaults used when a caller omits explicit limits. Production
# values are driven by ``RateLimitConfig`` through the dependency container.
DEFAULT_MAX_REQUESTS_PER_WINDOW = 60
DEFAULT_WINDOW_SECONDS = 60


class InMemoryRateLimiter:
    def __init__(
        self,
        max_requests: int = DEFAULT_MAX_REQUESTS_PER_WINDOW,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, subject: str) -> bool:
        now = time.time()
        bucket = self.buckets[subject]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True
