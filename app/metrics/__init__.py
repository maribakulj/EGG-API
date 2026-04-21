"""Prometheus metrics registry and helpers.

All metrics live in a dedicated ``CollectorRegistry`` so the test suite can
reset it between runs; in production the default registry is fine but tests
that instantiate the FastAPI app repeatedly would otherwise raise
``Duplicated timeseries`` errors from the global registry.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# Shared registry used by both app middleware and the /metrics endpoint.
registry = CollectorRegistry()

# --- HTTP request metrics ----------------------------------------------------

request_count = Counter(
    "egg_requests_total",
    "Total HTTP requests handled by EGG-API.",
    labelnames=("endpoint", "method", "status"),
    registry=registry,
)

request_duration = Histogram(
    "egg_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("endpoint", "method"),
    # Buckets tuned for typical EGG latency (ms range) with a long tail.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=registry,
)

# --- Backend and policy counters --------------------------------------------

backend_errors = Counter(
    "egg_backend_errors_total",
    "Backend call failures grouped by error code.",
    labelnames=("error_code",),
    registry=registry,
)

rate_limit_hits = Counter(
    "egg_rate_limit_hits_total",
    "Number of requests rejected by a rate limiter.",
    labelnames=("scope",),
    registry=registry,
)

# Silent-failure visibility: pre-Sprint-10 two fail-open paths swallowed
# errors without surfacing a metric — a broken Redis or a corrupted
# SQLite could go unnoticed until traffic spiked. These counters let
# the Prometheus alerts notice.

rate_limit_redis_errors = Counter(
    "egg_rate_limit_redis_errors_total",
    "Redis errors swallowed by the rate limiter's fail-open path.",
    labelnames=("scope",),
    registry=registry,
)

usage_persist_errors = Counter(
    "egg_usage_persist_errors_total",
    "Failures while writing a usage_events row from the audit middleware.",
    registry=registry,
)


def render_latest() -> tuple[bytes, str]:
    """Return (body, content-type) for the ``/metrics`` endpoint response."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
