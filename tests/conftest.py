from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.api_keys import ApiKeyManager
from app.dependencies import container
from app.rate_limit.limiter import InMemoryRateLimiter
from app.config.models import AppConfig
from app.mappers.schema_mapper import SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
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

    def translate_query(self, query: NormalizedQuery) -> dict[str, Any]:
        return {"query": query.model_dump(mode="python")}

    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        return {
            "hits": {
                "total": {"value": 1},
                "hits": [{"_source": {"id": "1", "type": "object", "title": "Test title", "creator_csv": "A;B"}}],
            },
            "aggregations": {"type": {"buckets": [{"key": "object", "doc_count": 1}]}}
        }

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        if record_id == "missing":
            return None
        return {"id": record_id, "type": "object", "title": "By ID"}

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        return {"type": {"object": 1}}


@pytest.fixture(autouse=True)
def reset_container(tmp_path) -> None:
    container.adapter = FakeAdapter()
    container.rate_limiter = InMemoryRateLimiter()
    container.config_manager._config = AppConfig()
    container.store = SQLiteStore(tmp_path / "state.sqlite3")
    container.store.initialize()
    container.api_keys = ApiKeyManager(container.store, container.config_manager.config.auth.bootstrap_admin_key)
    container.mapper = SchemaMapper(container.config_manager.config)
    container.policy = QueryPolicyEngine(container.config_manager.config)
    yield


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"x-api-key": container.api_keys.default_admin_key}
