"""Regression tests for Sprint 1 critical hardening (S1.1 - S1.11)."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.dependencies import container
from app.errors import AppError
from app.logging.request_context import get_request_id
from app.mappers.schema_mapper import SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.schemas.query import NormalizedQuery

# ---------------------------------------------------------------------------
# S1.1 — rate-limit subject must never be the raw API key
# ---------------------------------------------------------------------------


def test_s1_1_rate_limit_bucket_uses_key_id_not_raw_secret(client) -> None:
    created = container.api_keys.create("bucket-test")
    raw_secret = created.key

    container.config_manager.config.auth.public_mode = "api_key_required"
    try:
        client.get("/v1/search?q=x", headers={"x-api-key": raw_secret})
    finally:
        container.config_manager.config.auth.public_mode = "anonymous_allowed"

    # The in-memory bucket map must never contain the raw secret.
    assert raw_secret not in container.rate_limiter.buckets
    # It must contain the resolved key_id prefix instead.
    assert any(k.startswith("key:bucket-test") for k in container.rate_limiter.buckets)


def test_s1_1_invalid_key_falls_back_to_ip_bucket(client) -> None:
    client.get("/v1/search?q=x", headers={"x-api-key": "not-a-real-key"})
    # TestClient reports "testclient" as the client host.
    assert any(k.startswith("ip:") for k in container.rate_limiter.buckets)
    assert "not-a-real-key" not in container.rate_limiter.buckets


# ---------------------------------------------------------------------------
# S1.7 — x-request-id validation
# ---------------------------------------------------------------------------


def test_s1_7_rejects_whitespace_in_request_id(client) -> None:
    response = client.get("/v1/livez", headers={"x-request-id": "abc\ninjected-header"})
    # The handler still runs; only the injected id is discarded. The response
    # should carry a generated UUID rather than the injected value.
    # (We can't easily read the server-side ctx here, but the error-path
    # JSON shape includes the request_id; exercising get_request_id directly.)
    assert response.status_code == 200


def test_s1_7_get_request_id_fallback_on_invalid_input() -> None:
    class _Req:
        def __init__(self, value: str) -> None:
            self.headers = {"x-request-id": value}

    # Oversized input -> fallback UUID.
    rid = get_request_id(_Req("x" * 200))
    assert len(rid) == 36  # uuid4 length
    # Control chars -> fallback.
    rid = get_request_id(_Req("bad\nvalue"))
    assert len(rid) == 36
    # Valid shape passes through.
    rid = get_request_id(_Req("req-abc_123"))
    assert rid == "req-abc_123"


# ---------------------------------------------------------------------------
# S1.8 — input-size caps
# ---------------------------------------------------------------------------


def test_s1_8_q_length_cap_enforced(client) -> None:
    too_long = "a" * (QueryPolicyEngine.MAX_Q_LENGTH + 1)
    response = client.get("/v1/search", params={"q": too_long})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_parameter"
    assert body["error"]["details"]["max_length"] == QueryPolicyEngine.MAX_Q_LENGTH


def test_s1_8_filter_value_count_cap_enforced(client) -> None:
    values = [("q", "x")] + [
        ("type", f"v{i}") for i in range(QueryPolicyEngine.MAX_FILTER_VALUES + 1)
    ]
    response = client.get("/v1/search", params=values)
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["details"]["filter"] == "type"
    assert body["error"]["details"]["max"] == QueryPolicyEngine.MAX_FILTER_VALUES


def test_s1_8_filter_value_length_cap_enforced(client) -> None:
    huge_value = "z" * (QueryPolicyEngine.MAX_FILTER_VALUE_LENGTH + 1)
    response = client.get("/v1/search", params={"q": "x", "type": huge_value})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["filter"] == "type"


def test_s1_8_include_fields_count_cap_enforced(client) -> None:
    fields = ",".join([f"f{i}" for i in range(QueryPolicyEngine.MAX_INCLUDE_FIELDS + 1)])
    response = client.get("/v1/search", params={"q": "x", "include_fields": fields})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["max"] == QueryPolicyEngine.MAX_INCLUDE_FIELDS


# ---------------------------------------------------------------------------
# S1.3 — SchemaMapper returns 502 when id is unrecoverable
# ---------------------------------------------------------------------------


def test_s1_3_mapper_raises_bad_gateway_on_missing_id() -> None:
    mapper = SchemaMapper(container.config_manager.config)
    with pytest.raises(AppError) as exc:
        # Doc has neither `id` nor `_id`: structural field cannot be synthesized.
        mapper.map_record({"type": "object", "title": "no-id"})
    assert exc.value.code == "bad_gateway"
    assert exc.value.status_code == 502


def test_s1_3_mapper_uses_underscore_id_fallback() -> None:
    mapper = SchemaMapper(container.config_manager.config)
    rec = mapper.map_record({"_id": "fallback-1", "type": "object"})
    assert rec.id == "fallback-1"


# ---------------------------------------------------------------------------
# S1.2 — Prometheus label uses route template, not raw path
# ---------------------------------------------------------------------------


def test_s1_2_metrics_label_uses_route_template(client, admin_headers) -> None:
    # Hit the same record template multiple times with different ids.
    for record_id in ("abc", "def", "ghi"):
        client.get(f"/v1/records/{record_id}")

    # Scrape metrics using the admin key (S1.11 protection).
    metrics_response = client.get("/metrics", headers=admin_headers)
    assert metrics_response.status_code == 200
    body = metrics_response.text
    # The template path must appear, the raw paths must not be label values.
    assert 'endpoint="/v1/records/{record_id}"' in body
    assert 'endpoint="/v1/records/abc"' not in body


# ---------------------------------------------------------------------------
# S1.4 — audit middleware persists usage events even on handler exception
# ---------------------------------------------------------------------------


def test_s1_4_audit_logs_request_even_when_handler_raises(client) -> None:
    class _Boom:
        def search(self, _q: NormalizedQuery) -> dict[str, Any]:
            raise RuntimeError("backend exploded")

        def health(self) -> dict[str, Any]:
            return {"status": "red"}

        def list_sources(self) -> list[str]:
            return ["records"]

        def get_record(self, _id: str) -> None:
            return None

        def get_facets(self, _q: NormalizedQuery) -> dict[str, dict[str, int]]:
            return {}

        @staticmethod
        def extract_facets(_payload: dict[str, Any]) -> dict[str, dict[str, int]]:
            return {}

    original = container.adapter
    container.adapter = _Boom()
    try:
        # Handler raises -> FastAPI returns 500 -> audit middleware still fires.
        try:
            client.get("/v1/search?q=x")
        except RuntimeError:
            # TestClient re-raises the exception by default; we only care
            # that the usage event got persisted.
            pass
        events = container.store.list_recent_usage_events(limit=5)
        assert any(e.endpoint == "/v1/search" and e.status_code == 500 for e in events)
    finally:
        container.adapter = original


# ---------------------------------------------------------------------------
# S1.5 — Container.reload() closes the previous httpx client
# ---------------------------------------------------------------------------


def test_s1_5_reload_closes_previous_httpx_client(tmp_path) -> None:
    from app.config.models import AppConfig

    # Swap in a real ES adapter so we have an actual httpx.Client to observe.
    real_adapter = ElasticsearchAdapter("http://es.local", "records")
    container.adapter = real_adapter
    assert not real_adapter.client.is_closed

    new_cfg = container.config_manager.config.model_copy(deep=True)
    new_cfg.storage.sqlite_path = str(tmp_path / "state.sqlite3")
    container.reload(AppConfig.model_validate(new_cfg.model_dump(mode="python")))

    # After reload, the old adapter's client is closed; the new one is fresh.
    assert real_adapter.client.is_closed
    assert container.adapter is not real_adapter
    assert not container.adapter.client.is_closed


# ---------------------------------------------------------------------------
# S1.9 — /v1/livez is public, /v1/readyz is admin-gated
# ---------------------------------------------------------------------------


def test_s1_9_livez_is_public(client) -> None:
    response = client.get("/v1/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_s1_9_readyz_requires_admin_key(client) -> None:
    response = client.get("/v1/readyz")
    assert response.status_code == 401


def test_s1_9_readyz_returns_backend_health_for_admin(client, admin_headers) -> None:
    response = client.get("/v1/readyz", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "backend" in body


# ---------------------------------------------------------------------------
# S1.10 — /docs and /redoc and /openapi.json hidden in production
# ---------------------------------------------------------------------------


def test_s1_10_docs_exposed_outside_production(client) -> None:
    # tests/conftest doesn't set EGG_ENV=production
    response = client.get("/docs")
    assert response.status_code in (200, 307, 308)


def test_s1_10_production_flag_hides_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fresh app instance with EGG_ENV=production set before import.
    import importlib
    import sys

    monkeypatch.setenv("EGG_ENV", "production")
    # Drop the cached app to force re-evaluation of is_production().
    for mod in list(sys.modules):
        if mod.startswith("app.main"):
            del sys.modules[mod]
    import app.main as _main

    importlib.reload(_main)

    assert _main.app.docs_url is None
    assert _main.app.redoc_url is None
    assert _main.app.openapi_url is None


# ---------------------------------------------------------------------------
# S1.11 — /metrics requires auth when EGG_METRICS_TOKEN is set
# ---------------------------------------------------------------------------


def test_s1_11_metrics_token_allows_scraping(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_METRICS_TOKEN", "prom-secret-123")
    response = client.get("/metrics", headers={"Authorization": "Bearer prom-secret-123"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_s1_11_metrics_rejects_wrong_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_METRICS_TOKEN", "prom-secret-123")
    response = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_s1_11_metrics_admin_key_also_works(client, admin_headers) -> None:
    response = client.get("/metrics", headers=admin_headers)
    assert response.status_code == 200


def test_s1_11_metrics_open_in_dev_when_no_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EGG_METRICS_TOKEN", raising=False)
    # tests default to EGG_ENV=development; scraping without creds stays open
    # for the local dev workflow.
    response = client.get("/metrics")
    assert response.status_code == 200
