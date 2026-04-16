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
    return {"status": "ok", "backend": container.adapter.health()}


@router.get("/search", response_model=SearchResponse, response_model_exclude_none=False)
def search(
    request: Request,
    response: Response,
    _: None = Depends(enforce_public_auth),
):
    nq = container.policy.parse(request)
    etag = f'"search:{container.policy.compute_cache_key(nq)}"'
    cached = apply_cache_headers(request, response, etag)
    if cached is not None:
        return cached

    # Single backend call: aggregations are extracted from the same payload
    # when the client requested facets (no extra round-trip).
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
    nq = container.policy.parse(request)
    etag = f'"facets:{container.policy.compute_cache_key(nq)}"'
    cached = apply_cache_headers(request, response, etag)
    if cached is not None:
        return cached
    return {"facets": container.adapter.get_facets(nq)}
