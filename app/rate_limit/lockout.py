"""Per-IP lockout after repeated 401s on the public API (Sprint 18).

The request-rate limiter caps total traffic per subject. Credential
stuffing against ``/v1/*`` (with ``api_key_required``) looks like
*valid* traffic from the limiter's point of view but triggers 401s
each time. This counter tracks 401 responses per client IP within a
sliding window; once the threshold is crossed the middleware shortcuts
further requests from the same IP with 429 until the window expires.

Thread-safe via an RLock; in-process by design (multi-worker
deployments should pair this with the Redis rate limiter — Sprint 12
enforces that combo).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class PublicAuthLockout:
    """Sliding-window 401 counter keyed by client IP."""

    def __init__(self, *, threshold: int, window_seconds: int) -> None:
        self.threshold = max(0, int(threshold))
        self.window_seconds = max(1, int(window_seconds))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def record_failure(self, subject: str) -> None:
        """Store a 401 timestamp for ``subject`` (typically the client IP)."""
        if self.threshold <= 0:
            return
        now = time.monotonic()
        with self._lock:
            bucket = self._events[subject]
            self._evict(bucket, now)
            bucket.append(now)

    def is_locked(self, subject: str) -> bool:
        """Return True when ``subject`` has crossed the 401 threshold."""
        if self.threshold <= 0:
            return False
        with self._lock:
            bucket = self._events.get(subject)
            if not bucket:
                return False
            now = time.monotonic()
            self._evict(bucket, now)
            return len(bucket) >= self.threshold

    def reset(self, subject: str) -> None:
        with self._lock:
            self._events.pop(subject, None)

    def _evict(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
