from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_tmp_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("PISCO_STATE_DB_PATH", str(Path(_tmp_dir.name) / "test_state.sqlite3"))
os.environ.setdefault("PISCO_BOOTSTRAP_ADMIN_KEY", "test-admin-key")

from app.dependencies import container  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.query import NormalizedQuery  # noqa: E402


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
def reset_container() -> None:
    container.adapter = FakeAdapter()
    with container.store._connect() as conn:  # noqa: SLF001
        conn.execute("DELETE FROM usage_events")
        conn.execute("DELETE FROM quota_counters")
        conn.execute("DELETE FROM quota_config")
    container.rate_limiter.max_requests = 60
    container.rate_limiter.window_seconds = 60
    yield


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"x-api-key": container.api_keys.default_admin_key}
