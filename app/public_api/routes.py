from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.auth.dependencies import enforce_public_auth
from app.dependencies import container
from app.errors import AppError
from app.http_cache import apply_cache_headers
from app.schemas.record import Record, SearchResponse

router = APIRouter(prefix="/v1", tags=["public"])


@router.get("/health")
def health() -> dict[str, object]:
    """Liveness + backend health probe."""
    return {"status": "ok", "backend": container.adapter.health()}


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
    """
    nq = container.policy.parse(request)
    etag = f'"search:{container.policy.compute_cache_key(nq)}"'
    cached = apply_cache_headers(request, response, etag)
    if cached is not None:
        return cached

    payload = container.adapter.search(nq)
    hits = payload.get("hits", {}).get("hits", [])
    results = [container.mapper.map_record(h.get("_source", {})) for h in hits]
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
    record = container.mapper.map_record(raw)
    if not record.id or not record.type:
        raise AppError("configuration_error", "Required structural fields missing", status_code=500)
    return record


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
# Optional V1 endpoints (SPECS.md §12). /collections and /schema are active;
# /suggest and /manifest/{id} are declared but return 501 until their backend
# plumbing is contributed — this keeps the OpenAPI surface honest.
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


@router.get("/suggest")
def suggest(_: None = Depends(enforce_public_auth)) -> dict[str, object]:
    """Autocomplete suggestions (SPECS §12.2). Not yet implemented."""
    raise AppError(
        "not_implemented",
        "The /v1/suggest endpoint is declared but not yet implemented.",
        {"spec": "SPECS.md §12.2"},
        status_code=501,
    )


@router.get("/manifest/{record_id}")
def manifest(record_id: str, _: None = Depends(enforce_public_auth)) -> dict[str, object]:
    """IIIF manifest passthrough (SPECS §12.3). Not yet implemented."""
    raise AppError(
        "not_implemented",
        "The /v1/manifest/{id} endpoint is declared but not yet implemented.",
        {"spec": "SPECS.md §12.3", "record_id": record_id},
        status_code=501,
    )
