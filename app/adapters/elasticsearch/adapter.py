from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.errors import AppError
from app.schemas.query import NormalizedQuery

logger = logging.getLogger(__name__)

_TRANSIENT_HTTP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


class ElasticsearchAdapter:
    def __init__(
        self,
        base_url: str,
        index: str,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.index = index
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        # follow_redirects=False blocks SSRF via backend redirects to untrusted hosts.
        self.client = client or httpx.Client(
            timeout=float(timeout_seconds),
            follow_redirects=False,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue a request with bounded retries on transient failures/5xx.

        Raises :class:`AppError` ("backend_unavailable", 503) after exhaustion.
        """
        attempts = self.max_retries + 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                response = self.client.request(method, url, **kwargs)
            except _TRANSIENT_HTTP_EXCEPTIONS as exc:
                last_exc = exc
                logger.warning(
                    "backend transient error (attempt %d/%d) on %s %s: %s",
                    attempt + 1,
                    attempts,
                    method,
                    url,
                    exc,
                )
                if attempt + 1 >= attempts:
                    raise AppError(
                        "backend_unavailable",
                        "Backend is unavailable",
                        {"reason": str(exc)},
                        503,
                    ) from exc
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
                continue

            if response.status_code >= 500 and attempt + 1 < attempts:
                logger.warning(
                    "backend 5xx (attempt %d/%d) on %s %s: status=%d",
                    attempt + 1,
                    attempts,
                    method,
                    url,
                    response.status_code,
                )
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
                continue
            return response

        # Should be unreachable — fall back to a typed error.
        raise AppError(
            "backend_unavailable",
            "Backend is unavailable",
            {"reason": str(last_exc) if last_exc else "exhausted"},
            503,
        )

    def detect(self) -> dict[str, Any]:
        response = self._request("GET", f"{self.base_url}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Could not detect backend",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return {"detected": True, "version": response.json().get("version", {})}

    def health(self) -> dict[str, Any]:
        response = self._request("GET", f"{self.base_url}/_cluster/health")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend is unavailable",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return response.json()

    def list_sources(self) -> list[str]:
        return [self.index]

    def scan_fields(self) -> dict[str, Any]:
        response = self._request("GET", f"{self.base_url}/{self.index}/_mapping")
        response.raise_for_status()
        return response.json()

    def validate_mapping(self) -> dict[str, Any]:
        return {"status": "ok"}

    def translate_query(
        self,
        query: NormalizedQuery,
        *,
        include_aggs: bool = True,
        size_override: int | None = None,
        max_buckets_per_facet: int = 20,
    ) -> dict[str, Any]:
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

        size = size_override if size_override is not None else query.page_size
        bucket_size = max(1, int(max_buckets_per_facet))
        body: dict[str, Any] = {
            "from": (query.page - 1) * query.page_size,
            "size": size,
            "query": {
                "bool": {"must": must or [{"match_all": {}}], "filter": filter_clauses}
            },
        }
        if include_aggs and query.facets:
            body["aggs"] = {
                facet: {"terms": {"field": facet, "size": bucket_size}}
                for facet in query.facets
            }
        return body

    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        payload = self.translate_query(query)
        response = self._request(
            "POST", f"{self.base_url}/{self.index}/_search", json=payload
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend search failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return response.json()

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        response = self._request(
            "GET", f"{self.base_url}/{self.index}/_doc/{record_id}"
        )
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend record lookup failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        body = response.json()
        return body.get("_source")

    @staticmethod
    def extract_facets(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        """Extract facet counts from a raw search payload's ``aggregations``."""
        aggs = payload.get("aggregations", {}) or {}
        result: dict[str, dict[str, int]] = {}
        for facet, values in aggs.items():
            buckets = values.get("buckets", []) if isinstance(values, dict) else []
            result[facet] = {b["key"]: b["doc_count"] for b in buckets}
        return result

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        """Aggregations-only search (size=0) — use for the /v1/facets endpoint."""
        payload = self.translate_query(query, size_override=0)
        response = self._request(
            "POST", f"{self.base_url}/{self.index}/_search", json=payload
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend facet lookup failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return self.extract_facets(response.json())
