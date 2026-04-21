"""Regression tests for Sprint 7 architecture + extensibility (S7.1 - S7.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import BackendAdapter
from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.adapters.factory import build_adapter
from app.adapters.opensearch.adapter import OpenSearchAdapter
from app.config.models import AppConfig
from app.schemas.query import NormalizedQuery
from app.storage.base import KeyStore, SessionStore, StatsReporter, UsageLogger
from app.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# S7.1 — BackendAdapter Protocol
# ---------------------------------------------------------------------------


def test_s7_1_elasticsearch_adapter_satisfies_protocol() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records")
    assert isinstance(adapter, BackendAdapter)


def test_s7_1_opensearch_adapter_satisfies_protocol() -> None:
    adapter = OpenSearchAdapter("http://os.local", "records")
    assert isinstance(adapter, BackendAdapter)


def test_s7_1_fake_adapter_satisfies_protocol() -> None:
    # FakeAdapter now lives in ``tests._fakes`` (a real importable
    # module) so we can assert conformance explicitly rather than
    # reaching through a fixture's side effect on ``container.adapter``.
    from tests._fakes import FakeAdapter

    assert isinstance(FakeAdapter(), BackendAdapter)


# ---------------------------------------------------------------------------
# S7.4 — OpenSearchAdapter
# ---------------------------------------------------------------------------


def test_s7_4_opensearch_detect_accepts_1x() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"version": {"number": "2.11.0", "distribution": "opensearch"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenSearchAdapter(
        "http://os.local",
        "records",
        client=client,
        max_retries=0,
        retry_backoff_seconds=0,
    )
    result = adapter.detect()
    assert result["detected"] is True
    assert result["distribution"] == "opensearch"
    assert result["version"]["number"] == "2.11.0"


def test_s7_4_opensearch_rejects_zero_major() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": {"number": "0.9.0"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenSearchAdapter(
        "http://os.local",
        "records",
        client=client,
        max_retries=0,
        retry_backoff_seconds=0,
    )
    import pytest

    from app.errors import AppError

    with pytest.raises(AppError) as exc:
        adapter.detect()
    assert exc.value.code == "unsupported_backend_version"


def test_s7_4_opensearch_search_returns_raw_payload() -> None:
    payload: dict[str, Any] = {"hits": {"total": {"value": 0}, "hits": []}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenSearchAdapter(
        "http://os.local",
        "records",
        client=client,
        max_retries=0,
        retry_backoff_seconds=0,
    )
    assert adapter.search(NormalizedQuery(q="x")) == payload


# ---------------------------------------------------------------------------
# S7.5 — Adapter factory
# ---------------------------------------------------------------------------


def test_s7_5_factory_returns_elasticsearch_for_default_config() -> None:
    adapter = build_adapter(AppConfig())
    assert isinstance(adapter, ElasticsearchAdapter)
    assert not isinstance(adapter, OpenSearchAdapter)


def test_s7_5_factory_returns_opensearch_when_backend_type_is_opensearch() -> None:
    cfg = AppConfig.model_validate({"backend": {"type": "opensearch"}})
    adapter = build_adapter(cfg)
    assert isinstance(adapter, OpenSearchAdapter)


def test_s7_5_factory_surfaces_retry_deadline_on_built_adapter() -> None:
    cfg = AppConfig.model_validate(
        {
            "backend": {
                "type": "elasticsearch",
                "retry_backoff_cap_seconds": 2.5,
                "retry_deadline_seconds": 7.0,
            }
        }
    )
    adapter = build_adapter(cfg)
    assert adapter.retry_backoff_cap_seconds == 2.5
    assert adapter.retry_deadline_seconds == 7.0


# ---------------------------------------------------------------------------
# S7.3 — Store role Protocols
# ---------------------------------------------------------------------------


def test_s7_3_sqlite_store_satisfies_every_role_protocol(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "proto.sqlite3")
    store.initialize()
    assert isinstance(store, KeyStore)
    assert isinstance(store, SessionStore)
    assert isinstance(store, UsageLogger)
    assert isinstance(store, StatsReporter)


def test_s7_3_narrow_role_accepts_narrow_stub() -> None:
    # A focused test double that only implements UsageLogger should still
    # pass an isinstance check — that's the whole point of splitting the
    # Protocols.
    class _UsageOnly:
        def log_usage_event(
            self,
            request_id,
            endpoint,
            method,
            status_code,
            api_key_id,
            subject,
            latency_ms,
            error_code,
        ):
            pass

        def list_recent_usage_events(self, limit=100, offset=0):
            return []

        def count_usage_events(self):
            return 0

        def usage_summary(self):
            return {"events": 0, "errors": 0, "active_keys": 0}

        def purge_usage_events_older_than(self, retention_days):
            return 0

    assert isinstance(_UsageOnly(), UsageLogger)
    # And it explicitly does NOT satisfy KeyStore:
    assert not isinstance(_UsageOnly(), KeyStore)


# ---------------------------------------------------------------------------
# S7.2 — Container via request.app.state (additive; singleton still works)
# ---------------------------------------------------------------------------


def test_s7_2_app_state_exposes_container(client) -> None:
    from app.main import app as fastapi_app

    assert hasattr(fastapi_app.state, "container")
    assert fastapi_app.state.container is not None


def test_s7_2_get_container_prefers_app_state(tmp_path, monkeypatch) -> None:
    from fastapi import FastAPI

    from app.dependencies import Container, container as module_singleton, get_container

    # Build a *real* Container on an isolated state DB. Pre-Sprint-10
    # the test used ``Container.__new__(Container)`` which only
    # exercised ``is`` identity against an uninitialized object — it
    # would have passed even if get_container secretly returned the
    # module singleton.
    monkeypatch.setenv("EGG_STATE_DB_PATH", str(tmp_path / "alt-state.sqlite3"))
    alt = Container()
    fresh = FastAPI()
    fresh.state.container = alt

    class _Req:
        app = fresh

    resolved = get_container(_Req())  # type: ignore[arg-type]
    assert resolved is alt
    assert resolved is not module_singleton
    # Sanity: the alternate container is fully initialized (not just a
    # sentinel), so a caller that reaches for one of its services gets
    # a real implementation.
    assert resolved.store is not module_singleton.store
    assert resolved.adapter is not None


def test_s7_2_get_container_falls_back_to_singleton() -> None:
    from fastapi import FastAPI

    from app.dependencies import container as module_singleton, get_container

    # A FastAPI without a container on state falls back to the module
    # singleton, so imports outside the request lifecycle still work.
    fresh = FastAPI()

    class _Req:
        app = fresh

    assert get_container(_Req()) is module_singleton  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# S7.7 — backends.md exists and references the contract points
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_s7_7_backends_doc_mentions_contract_points() -> None:
    doc = (_REPO_ROOT / "docs" / "backends.md").read_text()
    for marker in (
        "BackendAdapter",
        "translate_query",
        "extract_facets",
        "factory.py",
        "BackendType",
    ):
        assert marker in doc, f"docs/backends.md is missing reference to {marker!r}"
