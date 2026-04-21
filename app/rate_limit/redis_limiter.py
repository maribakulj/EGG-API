"""Redis-backed rate limiter (opt-in).

Enabled when ``EGG_RATE_LIMIT_REDIS_URL`` is set; otherwise the
in-memory fallback keeps working. The Redis driver is a **soft**
dependency — importing this module succeeds even without the ``redis``
package installed, but :func:`build_rate_limiter` falls through to the
in-memory limiter and emits a warning.

Strategy: fixed-window counter keyed by
``egg:rl:{scope}:{subject}:{window_start}`` with ``INCR`` + ``EXPIRE``.
Sliding windows are nicer in theory; ``INCR`` is cheaper on the wire and
fine enough for the single-worker+multi-worker crossover this limiter
targets (the tail imprecision is bounded by ``window_seconds``). A
Lua-script upgrade to a true sliding window is a later sprint.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.metrics import rate_limit_redis_errors
from app.rate_limit.limiter import InMemoryRateLimiter

logger = logging.getLogger("egg.rate_limit.redis")


class RedisRateLimiter:
    """Fixed-window rate limiter backed by a shared Redis.

    Compatible with :class:`~app.rate_limit.limiter.InMemoryRateLimiter` —
    exposes ``max_requests``, ``window_seconds``, and ``allow(subject)``,
    so the dependency container can swap either implementation in
    without the call sites noticing.
    """

    def __init__(
        self,
        *,
        redis_client: Any,
        max_requests: int,
        window_seconds: int,
        scope: str = "public",
    ) -> None:
        self.max_requests = int(max_requests)
        self.window_seconds = max(1, int(window_seconds))
        self._scope = scope
        self._redis = redis_client

    def allow(self, subject: str) -> bool:
        now = int(time.time())
        window_start = now - (now % self.window_seconds)
        key = f"egg:rl:{self._scope}:{subject}:{window_start}"
        try:
            # Pipeline the INCR + EXPIRE so a failure in EXPIRE doesn't
            # leak a counter that never expires.
            pipe = self._redis.pipeline()
            pipe.incr(key, 1)
            pipe.expire(key, self.window_seconds)
            count, _ = pipe.execute()
        except Exception:  # pragma: no cover - covered by the smoke test
            # Redis hiccups must not break the request path. Fail open:
            # log, bump the error counter so the Prometheus alert picks
            # up sustained outages, and let the caller through. The
            # build_rate_limiter helper handles permanent outages by
            # falling back to the in-memory limiter at construction.
            logger.exception("redis_rate_limit_failed")
            rate_limit_redis_errors.labels(scope=self._scope).inc()
            return True
        return int(count) <= self.max_requests


def build_rate_limiter(
    *,
    max_requests: int,
    window_seconds: int,
    scope: str = "public",
) -> RedisRateLimiter | InMemoryRateLimiter:
    """Return a limiter, preferring Redis when ``EGG_RATE_LIMIT_REDIS_URL`` is set."""
    url = os.getenv("EGG_RATE_LIMIT_REDIS_URL", "").strip()
    if not url:
        return InMemoryRateLimiter(max_requests=max_requests, window_seconds=window_seconds)
    try:
        import redis
    except ImportError:
        logger.warning(
            "EGG_RATE_LIMIT_REDIS_URL set but 'redis' package is not installed; "
            "install with `pip install -e '.[redis]'`. Falling back to in-memory limiter."
        )
        return InMemoryRateLimiter(max_requests=max_requests, window_seconds=window_seconds)
    try:
        client = redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        # Sanity-check the connection so a misconfigured URL fails loudly
        # at startup rather than silently on the hot path.
        client.ping()
    except Exception:
        logger.warning(
            "EGG_RATE_LIMIT_REDIS_URL=%s rejected our ping; falling back to in-memory limiter.",
            url,
        )
        return InMemoryRateLimiter(max_requests=max_requests, window_seconds=window_seconds)
    logger.info("rate_limit_redis_enabled url=%s scope=%s", url, scope)
    return RedisRateLimiter(
        redis_client=client,
        max_requests=max_requests,
        window_seconds=window_seconds,
        scope=scope,
    )
