"""Query Policy Engine.

Parses HTTP query parameters into a :class:`NormalizedQuery` while enforcing
the active :class:`SecurityProfile`: allow/deny lists for sorts, facets, and
``include_fields``; hard caps on page size and pagination depth; boolean and
integer parsing that never leaks raw exceptions to callers. The engine also
exposes :meth:`compute_cache_key` (stable SHA-256 over the normalized query)
for ETag generation, and :meth:`redact_for_logs` to strip ``q`` before the
structured logger serializes the event.
"""

from __future__ import annotations

import hashlib
import json
from typing import ClassVar

from fastapi import Request

from app.config.models import AppConfig, SecurityProfile
from app.errors import AppError
from app.schemas.query import NormalizedQuery


class QueryPolicyEngine:
    allowed_params: ClassVar[set[str]] = {
        "q",
        "page",
        "page_size",
        "sort",
        "facet",
        "include_fields",
        "format",
        "type",
        "collection",
        "language",
        "institution",
        "date_from",
        "date_to",
        "subject",
        "has_digital",
        "has_iiif",
    }

    filter_params: ClassVar[set[str]] = {"type", "collection", "language", "institution", "subject"}

    # Hard caps protecting the backend from oversized inputs, independent of
    # the per-profile policy knobs.
    MAX_Q_LENGTH: ClassVar[int] = 512
    MAX_FILTER_VALUES: ClassVar[int] = 50
    MAX_FILTER_VALUE_LENGTH: ClassVar[int] = 256
    MAX_INCLUDE_FIELDS: ClassVar[int] = 20

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @property
    def profile(self) -> SecurityProfile:
        return self.config.profiles[self.config.security_profile]

    def parse(self, request: Request) -> NormalizedQuery:
        unknown = set(request.query_params.keys()) - self.allowed_params
        if unknown:
            raise AppError(
                "invalid_parameter", "Unknown query parameter(s)", {"unknown": sorted(unknown)}
            )

        qp = request.query_params
        q = qp.get("q")
        if not q and not self.profile.allow_empty_query:
            raise AppError(
                "missing_parameter", "q is required for this profile", {"parameter": "q"}
            )
        if q is not None and len(q) > self.MAX_Q_LENGTH:
            raise AppError(
                "invalid_parameter",
                "q is too long",
                {"max_length": self.MAX_Q_LENGTH, "actual_length": len(q)},
            )

        try:
            page = int(qp.get("page", "1"))
            page_size = int(qp.get("page_size", str(self.profile.page_size_default)))
        except ValueError as exc:
            raise AppError(
                "invalid_parameter",
                "page and page_size must be integers",
                {"reason": str(exc)},
            ) from exc
        if page < 1 or page_size < 1:
            raise AppError("invalid_parameter", "page and page_size must be positive")
        if page_size > self.profile.page_size_max:
            raise AppError(
                "invalid_parameter",
                "page_size exceeds policy",
                {"max": self.profile.page_size_max, "requested": page_size},
            )
        # ES rejects reads past `from + size`; that's page * page_size here.
        requested_depth = page * page_size
        if requested_depth > self.profile.max_depth:
            raise AppError(
                "unsupported_operation",
                "Deep pagination is not supported",
                {"max_depth": self.profile.max_depth, "requested": requested_depth},
            )

        sort = qp.get("sort")
        if sort and sort not in self.config.allowed_sorts:
            raise AppError("forbidden", "Sort is not allowed", {"sort": sort})

        facets = qp.getlist("facet")
        if len(facets) > self.profile.max_facets:
            raise AppError(
                "invalid_parameter",
                "Too many facets requested",
                {"max_facets": self.profile.max_facets},
            )
        forbidden_facets = [f for f in facets if f not in self.config.allowed_facets]
        if forbidden_facets:
            raise AppError("forbidden", "Facet is not allowed", {"facets": forbidden_facets})

        include_fields = [x for x in qp.get("include_fields", "").split(",") if x]
        if len(include_fields) > self.MAX_INCLUDE_FIELDS:
            raise AppError(
                "invalid_parameter",
                "Too many include_fields",
                {"max": self.MAX_INCLUDE_FIELDS, "requested": len(include_fields)},
            )
        forbidden_fields = [
            f for f in include_fields if f not in self.config.allowed_include_fields
        ]
        if forbidden_fields:
            raise AppError(
                "forbidden",
                "include_fields contains forbidden fields",
                {"fields": forbidden_fields},
            )

        filters: dict[str, list[str]] = {}
        for name in self.filter_params:
            values = qp.getlist(name)
            if not values:
                continue
            if len(values) > self.MAX_FILTER_VALUES:
                raise AppError(
                    "invalid_parameter",
                    "Too many values for filter",
                    {
                        "filter": name,
                        "max": self.MAX_FILTER_VALUES,
                        "requested": len(values),
                    },
                )
            oversized = [v for v in values if len(v) > self.MAX_FILTER_VALUE_LENGTH]
            if oversized:
                raise AppError(
                    "invalid_parameter",
                    "Filter value too long",
                    {
                        "filter": name,
                        "max_length": self.MAX_FILTER_VALUE_LENGTH,
                    },
                )
            filters[name] = values

        return NormalizedQuery(
            q=q,
            page=page,
            page_size=page_size,
            sort=sort,
            facets=facets,
            include_fields=include_fields,
            filters=filters,
            date_from=qp.get("date_from"),
            date_to=qp.get("date_to"),
            has_digital=self._parse_bool(qp.get("has_digital")),
            has_iiif=self._parse_bool(qp.get("has_iiif")),
        )

    def compute_cache_key(self, nq: NormalizedQuery) -> str:
        # Pydantic v2's ``model_dump_json`` has no ``sort_keys``; use json.dumps
        # on the plain dict so the hash is stable regardless of dict ordering.
        payload = json.dumps(nq.model_dump(mode="python"), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def redact_for_logs(self, nq: NormalizedQuery) -> dict[str, object]:
        data = nq.model_dump(mode="python")
        if data.get("q"):
            data["q"] = "[redacted]"
        return data

    @staticmethod
    def _parse_bool(value: str | None) -> bool | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise AppError("invalid_parameter", "Invalid boolean value", {"value": value})
