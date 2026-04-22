"""Shared test doubles.

Lives outside ``conftest.py`` on purpose: ``conftest.py`` is a pytest
plugin, not a normal importable module, so depending on it from other
tests is fragile (and fails outright when ``tests/`` is not a package,
which is the default pytest layout). Put test helpers the suite needs
to import by name here instead.
"""

from __future__ import annotations

from typing import Any

from app.schemas.query import NormalizedQuery


class FakeAdapter:
    """In-memory test double for the ``BackendAdapter`` Protocol.

    Returns deterministic, non-empty fixtures so the public contract
    tests (search / records / facets / suggest) can run without a real
    Elasticsearch backend. Kept intentionally simple — a single hit,
    one facet, one suggestion family.

    ``bulk_index`` appends incoming documents to :attr:`stored` so
    Sprint 22 importer tests can assert what the importer sent.
    """

    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

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

    def suggest(self, prefix: str, limit: int = 10) -> list[str]:
        # Deterministic suggestions for the contract tests; real adapters
        # hit the backend's completion surface.
        if not prefix:
            return []
        return [f"{prefix} result {i}" for i in range(min(limit, 3))]

    def bulk_index(self, docs: list[dict[str, Any]]) -> tuple[int, int]:
        self.stored.extend(dict(d) for d in docs)
        return len(docs), 0

    @staticmethod
    def extract_facets(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        aggs = payload.get("aggregations", {}) or {}
        result: dict[str, dict[str, int]] = {}
        for facet, values in aggs.items():
            buckets = values.get("buckets", []) if isinstance(values, dict) else []
            result[facet] = {b["key"]: b["doc_count"] for b in buckets}
        return result
