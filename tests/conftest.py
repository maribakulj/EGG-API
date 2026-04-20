from __future__ import annotations

import os
import tempfile

# Pin env vars before any app modules load so the container doesn't
# generate a sidecar file under the repo working tree.
os.environ.setdefault("EGG_BOOTSTRAP_ADMIN_KEY", "test-admin-key-abcdefghijklmnop")
os.environ.setdefault("EGG_HOME", tempfile.mkdtemp(prefix="egg-test-home-"))

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.api_keys import ApiKeyManager
from app.config.models import AppConfig
from app.dependencies import container
from app.mappers.schema_mapper import SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import InMemoryRateLimiter
from app.schemas.query import NormalizedQuery
from app.storage.sqlite_store import SQLiteStore


class FakeAdapter:
    def detect(self) -> dict[str, Any]:
        return {"detected": True, "version": {"number": "8.0.0"}}

    def health(self) -> dict[str, Any]:
        return {"status": "green"}

    def list_sources(self) -> list[str]:
        return ["records"]

    def scan_fields(self) -> dict[str, Any]:
        return {"records": {"mappings": {"properties": {"title": {"type": "text"}}}}}

    def translate_query(self, query: NormalizedQuery, **_: Any) -> dict[str, Any]:
        return {"query": query.model_dump(mode="python")}

    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        return {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_source": {
                            "id": "1",
                            "type": "object",
                            "title": "Test title",
                            "creator_csv": "A;B",
                        }
                    }
                ],
            },
            "aggregations": {"type": {"buckets": [{"key": "object", "doc_count": 1}]}},
        }

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        if record_id == "missing":
            return None
        return {"id": record_id, "type": "object", "title": "By ID"}

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        return {"type": {"object": 1}}

    @staticmethod
    def extract_facets(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        aggs = payload.get("aggregations", {}) or {}
        result: dict[str, dict[str, int]] = {}
        for facet, values in aggs.items():
            buckets = values.get("buckets", []) if isinstance(values, dict) else []
            result[facet] = {b["key"]: b["doc_count"] for b in buckets}
        return result


@pytest.fixture(autouse=True)
def reset_container(tmp_path) -> None:
    container.adapter = FakeAdapter()
    container.rate_limiter = InMemoryRateLimiter()
    container.login_rate_limiter = InMemoryRateLimiter(max_requests=1000, window_seconds=60)
    cfg = AppConfig()
    # TestClient talks http://, so secure cookies would never round-trip.
    cfg.auth.admin_cookie_secure = False
    cfg.auth.admin_cookie_samesite = "lax"
    # Deterministic admin key for tests without requiring a sidecar file.
    cfg.auth.bootstrap_admin_key = "test-admin-key-abcdefghijklmnop"
    container.config_manager._config = cfg
    container.store = SQLiteStore(tmp_path / "state.sqlite3")
    container.store.initialize()
    container.api_keys = ApiKeyManager(container.store, cfg.auth.bootstrap_admin_key)
    container.mapper = SchemaMapper(cfg)
    container.policy = QueryPolicyEngine(cfg)
    yield


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"x-api-key": container.api_keys.default_admin_key}
