from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse

from app.auth.dependencies import enforce_public_auth, require_admin_key
from app.dependencies import container
from app.errors import AppError
from app.http_cache import apply_cache_headers
from app.schemas.record import Record, SearchResponse

router = APIRouter(prefix="/v1", tags=["public"])


# Columns surfaced in the CSV export for /v1/search. Keeping the list
# deterministic (and intentionally flat) lets a GLAM consumer pipe the
# response into a spreadsheet without parsing nested JSON.
_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "type",
    "title",
    "subtitle",
    "description",
    "creators",
    "languages",
    "subjects",
    "collection",
    "holding_institution",
)


def _csv_cell(record: Record, column: str) -> str:
    value: Any = getattr(record, column, "")
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        # Prefer a "label" if present (LabelRef), fall back to the raw id.
        return str(dumped.get("label") or dumped.get("id") or "")
    return str(value)


def _render_csv(results: list[Record]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(_CSV_COLUMNS)
    for record in results:
        writer.writerow([_csv_cell(record, col) for col in _CSV_COLUMNS])
    return buffer.getvalue()


@router.get("/livez")
def livez() -> dict[str, str]:
    """Liveness probe: does the process respond?

    Minimal by design — returns a constant body. Safe to expose publicly;
    no internal state leaks.
    """
    return {"status": "ok"}


@router.get("/readyz")
def readyz(_: str = Depends(require_admin_key)) -> dict[str, object]:
    """Readiness probe: can the service serve traffic?

    Admin-authenticated because it proxies the backend's internal cluster
    health — an operator wants detail; an anonymous caller must not fingerprint
    the upstream state (green/yellow/red, shard counts, cluster name).
    """
    return {"status": "ok", "backend": container.adapter.health()}


@router.get("/health", deprecated=True)
def health() -> dict[str, str]:
    """Deprecated — prefer ``/v1/livez`` (public) and ``/v1/readyz`` (admin).

    Retained as a minimal alias for backwards compatibility. Returns only the
    liveness payload; backend state is available via ``/v1/readyz``.
    """
    return {"status": "ok"}


@router.get("/search", response_model=SearchResponse, response_model_exclude_none=False)
def search(
    request: Request,
    response: Response,
    _: None = Depends(enforce_public_auth),
):
    """Run a full-text search.

    Applies the security profile (page size, facet allowlist, depth limit) before
    forwarding a single call to the backend; aggregations are extracted from the
    same response when the caller requested facets.

    ``format=csv`` switches the response to ``text/csv`` with a fixed,
    spreadsheet-friendly column set (no facet payload). The query-policy
    engine accepts and validates the parameter.
    """
    fmt = (request.query_params.get("format") or "json").lower()
    if fmt not in {"json", "csv"}:
        raise AppError(
            "invalid_parameter",
            "format must be one of json|csv",
            {"format": fmt},
        )

    nq = container.policy.parse(request)
    etag = f'"search:{fmt}:{container.policy.compute_cache_key(nq)}"'
    cached = apply_cache_headers(request, response, etag)
    if cached is not None:
        return cached

    payload = container.adapter.search(nq)
    hits = payload.get("hits", {}).get("hits", [])
    results = [container.mapper.map_record(h.get("_source", {})) for h in hits]

    if fmt == "csv":
        body = _render_csv(results)
        csv_response = PlainTextResponse(
            content=body,
            media_type="text/csv; charset=utf-8",
        )
        # Propagate the cache metadata set by apply_cache_headers; the fresh
        # response object does not inherit headers from the search handler's
        # placeholder `response`.
        for header in ("Cache-Control", "ETag"):
            if header in response.headers:
                csv_response.headers[header] = response.headers[header]
        csv_response.headers["Content-Disposition"] = 'attachment; filename="search.csv"'
        return csv_response

    facets = container.adapter.extract_facets(payload) if nq.facets else {}
    total_value = payload.get("hits", {}).get("total", {}).get("value", len(results))
    return SearchResponse(
        total=total_value,
        page=nq.page,
        page_size=nq.page_size,
        results=results,
        facets=facets,
    )


@router.get("/records/{record_id}", response_model=Record)
def get_record(
    record_id: str,
    request: Request,
    response: Response,
    _: None = Depends(enforce_public_auth),
):
    """Fetch a single record by identifier."""
    etag = f'"record:{record_id}"'
    cached = apply_cache_headers(request, response, etag)
    if cached is not None:
        return cached
    raw = container.adapter.get_record(record_id)
    if raw is None:
        raise AppError("not_found", "Record not found", status_code=404)
    # map_record raises AppError("bad_gateway", 502) if the backend record
    # is missing a usable id; that surfaces as the right status without this
    # extra guard.
    return container.mapper.map_record(raw)


@router.get("/facets")
def facets(
    request: Request,
    response: Response,
    _: None = Depends(enforce_public_auth),
):
    """Return facet counts only (no hits), useful for sidebar UIs."""
    nq = container.policy.parse(request)
    etag = f'"facets:{container.policy.compute_cache_key(nq)}"'
    cached = apply_cache_headers(request, response, etag)
    if cached is not None:
        return cached
    return {"facets": container.adapter.get_facets(nq)}


# ---------------------------------------------------------------------------
# Optional V1 endpoints (SPECS.md §12). /collections and /schema are active.
# /suggest and /manifest/{id} were removed in Sprint 5: they were declared-
# but-501 placeholders that only padded the OpenAPI surface. Add them back
# here when their backend plumbing lands so the public contract stays honest.
# ---------------------------------------------------------------------------


@router.get("/collections")
def collections(_: None = Depends(enforce_public_auth)) -> dict[str, object]:
    """Return publicly exposed collections (SPECS §12.1)."""
    sources = container.adapter.list_sources()
    return {"collections": [{"id": name, "label": name} for name in sources]}


@router.get("/schema")
def public_schema(_: None = Depends(enforce_public_auth)) -> dict[str, object]:
    """Return the active public schema for ``Record`` (SPECS §12.4).

    Only exposes the *activated* fields (mapping + allowed_include_fields)
    plus the facet and sort allowlists.
    """
    cfg = container.config_manager.config
    fields = []
    for name, rule in cfg.mapping.items():
        fields.append(
            {
                "name": name,
                "mode": rule.mode,
                "criticality": rule.criticality,
            }
        )
    return {
        "fields": fields,
        "allowed_include_fields": list(cfg.allowed_include_fields),
        "allowed_facets": list(cfg.allowed_facets),
        "allowed_sorts": list(cfg.allowed_sorts),
        "filters": sorted(container.policy.filter_params),
    }
