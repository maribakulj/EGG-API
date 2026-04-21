"""OpenSearch adapter.

OpenSearch forked from Elasticsearch 7.10; its ``/_search``, ``/_doc/{id}``,
``/_mapping`` and ``/_cluster/health`` REST surfaces are drop-in compatible
with the subset EGG uses. This adapter therefore reuses
:class:`~app.adapters.elasticsearch.adapter.ElasticsearchAdapter` wholesale
and only differs in:

  - ``detect()`` floor: OpenSearch starts its version numbering at 1.x,
    so the Elasticsearch ">= 7" gate would wrongly reject a healthy
    OpenSearch. We override the minimum to 1.
  - ``detect()`` advertises ``distribution: "opensearch"`` in the payload
    so the admin UI can label the backend accurately.

Everything else — retry loop with jitter, X-Opaque-Id propagation, facet
cap — is inherited unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.elasticsearch.adapter import (
    ElasticsearchAdapter,
    _parse_major_version,
    _record_backend_error,
    _tracing_headers,
)
from app.errors import AppError


class OpenSearchAdapter(ElasticsearchAdapter):
    _MIN_SUPPORTED_MAJOR_VERSION = 1

    def detect(self) -> dict[str, Any]:
        response = self._request("GET", self.base_url, headers=_tracing_headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _record_backend_error("backend_unavailable")
            raise AppError(
                "backend_unavailable",
                "Could not detect backend",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc

        body = response.json()
        version_info = body.get("version", {}) or {}
        version_number = str(version_info.get("number", ""))
        major = _parse_major_version(version_number)
        if major is not None and major < self._MIN_SUPPORTED_MAJOR_VERSION:
            _record_backend_error("unsupported_backend_version")
            raise AppError(
                "unsupported_backend_version",
                f"OpenSearch {version_number} is not supported; "
                f"requires {self._MIN_SUPPORTED_MAJOR_VERSION}+",
                {"version": version_number, "minimum_major": self._MIN_SUPPORTED_MAJOR_VERSION},
                503,
            )
        return {
            "detected": True,
            "distribution": version_info.get("distribution", "opensearch"),
            "version": version_info,
        }
