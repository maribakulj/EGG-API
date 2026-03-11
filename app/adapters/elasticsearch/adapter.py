from __future__ import annotations

from typing import Any

import httpx

from app.errors import AppError
from app.schemas.query import NormalizedQuery


class ElasticsearchAdapter:
    def __init__(self, base_url: str, index: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.index = index
        self.client = client or httpx.Client(timeout=5.0)

    def detect(self) -> dict[str, Any]:
        try:
            response = self.client.get(f"{self.base_url}")
            response.raise_for_status()
            return {"detected": True, "version": response.json().get("version", {})}
        except Exception as exc:  # noqa: BLE001
            raise AppError("backend_unavailable", "Could not detect backend", {"reason": str(exc)}, 503) from exc

    def health(self) -> dict[str, Any]:
        try:
            response = self.client.get(f"{self.base_url}/_cluster/health")
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise AppError("backend_unavailable", "Backend is unavailable", {"reason": str(exc)}, 503) from exc

    def list_sources(self) -> list[str]:
        return [self.index]

    def scan_fields(self) -> dict[str, Any]:
        response = self.client.get(f"{self.base_url}/{self.index}/_mapping")
        response.raise_for_status()
        return response.json()

    def validate_mapping(self) -> dict[str, Any]:
        return {"status": "ok"}

    def translate_query(self, query: NormalizedQuery) -> dict[str, Any]:
        must: list[dict[str, Any]] = []
        filter_clauses: list[dict[str, Any]] = []
        if query.q:
            must.append({"simple_query_string": {"query": query.q}})
        for field, values in query.filters.items():
            filter_clauses.append({"terms": {field: values}})
        if query.has_digital is not None:
            filter_clauses.append({"term": {"has_digital": query.has_digital}})
        if query.has_iiif is not None:
            filter_clauses.append({"term": {"has_iiif": query.has_iiif}})
        return {
            "from": (query.page - 1) * query.page_size,
            "size": query.page_size,
            "query": {"bool": {"must": must or [{"match_all": {}}], "filter": filter_clauses}},
            "aggs": {facet: {"terms": {"field": facet, "size": 20}} for facet in query.facets},
        }

    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        payload = self.translate_query(query)
        response = self.client.post(f"{self.base_url}/{self.index}/_search", json=payload)
        response.raise_for_status()
        return response.json()

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        response = self.client.get(f"{self.base_url}/{self.index}/_doc/{record_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        return body.get("_source")

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        data = self.search(query)
        aggs = data.get("aggregations", {})
        result: dict[str, dict[str, int]] = {}
        for facet, values in aggs.items():
            result[facet] = {b["key"]: b["doc_count"] for b in values.get("buckets", [])}
        return result
