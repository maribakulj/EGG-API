from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.auth.dependencies import enforce_public_auth
from app.dependencies import container
from app.errors import AppError
from app.schemas.record import Record, SearchResponse

router = APIRouter(prefix="/v1", tags=["public"])


@router.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "backend": container.adapter.health()}


@router.get("/search", response_model=SearchResponse)
def search(request: Request, _: None = Depends(enforce_public_auth)) -> SearchResponse:
    nq = container.policy.parse(request)
    payload = container.adapter.search(nq)
    hits = payload.get("hits", {}).get("hits", [])
    results = [container.mapper.map_record(h.get("_source", {})) for h in hits]
    facets = container.adapter.get_facets(nq) if nq.facets else {}
    total_value = payload.get("hits", {}).get("total", {}).get("value", len(results))
    return SearchResponse(total=total_value, page=nq.page, page_size=nq.page_size, results=results, facets=facets)


@router.get("/records/{record_id}", response_model=Record)
def get_record(record_id: str, _: None = Depends(enforce_public_auth)) -> Record:
    raw = container.adapter.get_record(record_id)
    if raw is None:
        raise AppError("not_found", "Record not found", status_code=404)
    record = container.mapper.map_record(raw)
    if not record.id or not record.type:
        raise AppError("configuration_error", "Required structural fields missing", status_code=500)
    return record


@router.get("/facets")
def facets(request: Request, _: None = Depends(enforce_public_auth)) -> dict[str, object]:
    nq = container.policy.parse(request)
    return {"facets": container.adapter.get_facets(nq)}
