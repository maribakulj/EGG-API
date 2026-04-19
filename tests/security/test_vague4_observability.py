"""Regression tests for Vague 4 (H9-H10, M1-M5): observability & storage."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter, _parse_major_version
from app.dependencies import container
from app.errors import AppError
from app.metrics import registry as metrics_registry
from app.schemas.query import NormalizedQuery
from app.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# H9 — Prometheus /metrics
# ---------------------------------------------------------------------------

def test_h9_metrics_endpoint_returns_prometheus_exposition(client) -> None:
    client.get("/v1/search?q=abc")
    response = client.get("/metrics")
    assert response.status_code == 200
    ct = response.headers.get("content-type", "")
    # prometheus-client ships different exposition content-types across
    # versions (0.0.4 and 1.0.0 both use text/plain); only assert the prefix.
    assert ct.startswith("text/plain") and "version=" in ct
    body = response.text
    # Counters & histograms we registered are present:
    assert "pisco_requests_total" in body
    assert 'endpoint="/v1/search"' in body
    assert "pisco_request_duration_seconds" in body


def test_h9_rate_limit_counter_increments_on_429(client) -> None:
    # Exhaust the public rate limiter quickly.
    container.rate_limiter.max_requests = 1
    container.rate_limiter.window_seconds = 60

    first = client.get("/v1/search?q=abc")
    second = client.get("/v1/search?q=abc")
    assert first.status_code == 200
    assert second.status_code == 429

    metrics = client.get("/metrics").text
    assert 'pisco_rate_limit_hits_total{scope="public"}' in metrics


def test_h9_backend_error_counter_increments_on_transient_failure() -> None:
    class _Boom(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=httpx.Client(transport=_Boom()),
        max_retries=0,
        retry_backoff_seconds=0,
    )

    # Snapshot the counter before, call the failing op, check the delta.
    from prometheus_client import generate_latest

    before = generate_latest(metrics_registry).decode()
    before_count = sum(
        1
        for line in before.splitlines()
        if line.startswith('pisco_backend_errors_total{error_code="backend_unavailable"}')
    )

    with pytest.raises(AppError):
        adapter.search(NormalizedQuery(q="x"))

    after = generate_latest(metrics_registry).decode()
    after_value = _extract_counter(after, 'pisco_backend_errors_total{error_code="backend_unavailable"}')
    before_value = _extract_counter(before, 'pisco_backend_errors_total{error_code="backend_unavailable"}')
    assert after_value > before_value
    # Confirm the metric is registered exactly once (no duplicate registry).
    assert before_count <= 1


def _extract_counter(exposition: str, series_prefix: str) -> float:
    for line in exposition.splitlines():
        if line.startswith(series_prefix):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


# ---------------------------------------------------------------------------
# H10 — structlog bootstrap
# ---------------------------------------------------------------------------

def test_h10_get_logger_returns_a_bound_logger() -> None:
    from app.logging import get_logger

    log = get_logger("pisco.test")
    # structlog BoundLogger exposes info/debug/warning and bind.
    assert callable(log.info)
    assert callable(log.warning)
    assert callable(log.bind)


def test_h10_logging_is_idempotent() -> None:
    from app.logging import configure

    configure()
    configure()  # second call must not raise or duplicate handlers
    import logging as stdlib_logging

    assert stdlib_logging.getLogger().handlers, "expected at least one handler"


# ---------------------------------------------------------------------------
# M1 — SQL indexes on hot columns
# ---------------------------------------------------------------------------

def test_m1_indexes_exist_after_initialize(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    names = {row["name"] for row in rows}
    expected = {
        "idx_api_keys_hash",
        "idx_usage_events_timestamp",
        "idx_usage_events_subject",
        "idx_usage_events_status",
        "idx_quota_counters_subject",
        "idx_ui_sessions_expires",
    }
    missing = expected - names
    assert not missing, f"missing indexes: {missing}"


# ---------------------------------------------------------------------------
# M2 — Paginated usage events
# ---------------------------------------------------------------------------

def test_m2_list_recent_usage_events_respects_offset(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    for i in range(5):
        store.log_usage_event(
            request_id=f"r{i}",
            endpoint="/v1/search",
            method="GET",
            status_code=200,
            api_key_id=None,
            subject="tester",
            latency_ms=i,
            error_code=None,
        )
    page1 = store.list_recent_usage_events(limit=2, offset=0)
    page2 = store.list_recent_usage_events(limit=2, offset=2)
    assert [e.latency_ms for e in page1] == [4, 3]
    assert [e.latency_ms for e in page2] == [2, 1]
    assert store.count_usage_events() == 5


def test_m2_admin_usage_endpoint_paginates(client, admin_headers) -> None:
    # Ensure there are a few events to paginate through.
    for _ in range(3):
        client.get("/v1/search?q=abc")
    response = client.get(
        "/admin/v1/usage?limit=1&offset=0", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"total", "limit", "offset", "events"}
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["events"]) == 1


def test_m2_admin_usage_rejects_out_of_range(client, admin_headers) -> None:
    response = client.get(
        "/admin/v1/usage?limit=0", headers=admin_headers
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# M5 — ES version check
# ---------------------------------------------------------------------------

def _es_detect_transport(body: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def test_m5_accepts_es_7_and_above() -> None:
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=httpx.Client(transport=_es_detect_transport({"version": {"number": "8.12.0"}})),
        max_retries=0,
        retry_backoff_seconds=0,
    )
    result = adapter.detect()
    assert result["detected"] is True
    assert result["version"]["number"] == "8.12.0"


def test_m5_rejects_es_6() -> None:
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=httpx.Client(transport=_es_detect_transport({"version": {"number": "6.8.0"}})),
        max_retries=0,
        retry_backoff_seconds=0,
    )
    with pytest.raises(AppError) as excinfo:
        adapter.detect()
    assert excinfo.value.code == "unsupported_backend_version"
    assert excinfo.value.status_code == 503
    assert excinfo.value.details["version"] == "6.8.0"


def test_m5_parse_major_version_tolerates_garbage() -> None:
    assert _parse_major_version("7.17.0") == 7
    assert _parse_major_version("") is None
    assert _parse_major_version("abc") is None
    assert _parse_major_version("9") == 9


def test_m5_missing_version_does_not_crash() -> None:
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=httpx.Client(transport=_es_detect_transport({"unknown": {}})),
        max_retries=0,
        retry_backoff_seconds=0,
    )
    # No version => accept (can't prove it's too old).
    result = adapter.detect()
    assert result["detected"] is True
