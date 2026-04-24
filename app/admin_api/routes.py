from __future__ import annotations

from dataclasses import asdict

import yaml
from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app.auth.dependencies import require_admin_key
from app.config.models import AppConfig
from app.dependencies import container
from app.errors import AppError

router = APIRouter(prefix="/admin/v1", tags=["admin"], dependencies=[Depends(require_admin_key)])


@router.post("/setup/detect")
def detect() -> dict[str, object]:
    """Probe the backend and return its detected version metadata."""
    return container.adapter.detect()


@router.post("/setup/scan-fields")
def scan_fields() -> dict[str, object]:
    """Return the backend index mapping for field discovery."""
    return container.adapter.scan_fields()


@router.post("/setup/create-config")
def create_config(payload: dict[str, object] = Body(default_factory=dict)) -> dict[str, object]:
    """Replace the current configuration with ``payload`` (validated)."""
    cfg = AppConfig.model_validate(payload or {})
    container.reload(cfg)
    return {"status": "created"}


@router.get("/config")
def get_config() -> dict[str, object]:
    """Return the current configuration with secrets masked.

    Admins see which secrets are configured (``"***"`` sentinel) without
    receiving the cleartext values. The canonical redaction registry
    lives on :class:`ConfigManager` and is shared with the YAML save
    path so the two cannot drift.
    """
    data = container.config_manager.config.model_dump(mode="python")
    return container.config_manager.redact(data, mask=True)


@router.put("/config")
def put_config(payload: dict[str, object]) -> dict[str, object]:
    """Persist a full configuration replacement."""
    cfg = AppConfig.model_validate(payload)
    container.reload(cfg)
    return {"status": "updated"}


@router.post("/config/validate")
def validate_config(payload: dict[str, object]) -> dict[str, object]:
    """Dry-run validation for a candidate configuration payload."""
    ok, error = container.config_manager.validate_data(payload)
    return {"valid": ok, "error": error}


@router.post("/test-query")
def test_query(request: Request) -> dict[str, object]:
    """Translate the provided query into the backend DSL without executing it."""
    nq = container.policy.parse(request)
    return {"translated": container.adapter.translate_query(nq)}


@router.get("/debug/translate")
def debug_translate(request: Request) -> dict[str, object]:
    """Inspect how a query string would be parsed and translated.

    Mirrors ``/admin/v1/test-query`` but accepts the same GET query-string
    an operator would actually reproduce (``/v1/search?q=…``). Returns the
    normalized query, the ETag cache key, and the backend DSL the adapter
    would send — without touching the backend. Useful when a caller
    reports unexpected results and you want to eyeball the pipeline.
    """
    nq = container.policy.parse(request)
    return {
        "normalized": nq.model_dump(mode="python"),
        "cache_key": container.policy.compute_cache_key(nq),
        "translated": container.adapter.translate_query(nq),
    }


@router.get("/usage")
def usage_events(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """Paginated listing of recent usage events."""
    events = container.store.list_recent_usage_events(limit=limit, offset=offset)
    return {
        "total": container.store.count_usage_events(),
        "limit": limit,
        "offset": offset,
        "events": [asdict(e) for e in events],
    }


@router.get("/status")
def status() -> dict[str, object]:
    """Aggregate backend + mapping health for operator dashboards."""
    cfg = container.config_manager.config
    probe_doc = {"id": "probe", "type": "record"}
    mapping = container.mapping_health.classify(cfg.mapping, probe_doc)
    if any(v == "missing" for v in mapping.values()):
        raise AppError(
            "configuration_error",
            "Mapping has missing required/recommended sources",
            {"mapping": mapping},
            500,
        )
    return {"status": "ok", "sources": container.adapter.list_sources(), "mapping": mapping}


@router.get("/logs")
def logs(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    endpoint: str | None = Query(None),
    status_min: int | None = Query(None, ge=100, le=599),
    status_max: int | None = Query(None, ge=100, le=599),
    since: str | None = Query(None, description="ISO-8601 timestamp (inclusive lower bound)"),
    until: str | None = Query(None, description="ISO-8601 timestamp (inclusive upper bound)"),
    key_id: str | None = Query(None, description="Filter by api_key_id (public label)"),
) -> dict[str, object]:
    """Filterable structured-log query (SPECS §13.12).

    Wraps ``usage_events`` with the fields an operator actually needs
    during an incident: a time window, a status range, an endpoint or
    a specific caller. Omit every filter for a plain "tail" view.
    """
    events, total = container.store.query_usage_events(
        limit=limit,
        offset=offset,
        endpoint=endpoint,
        status_min=status_min,
        status_max=status_max,
        since=since,
        until=until,
        key_id=key_id,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {
            "endpoint": endpoint,
            "status_min": status_min,
            "status_max": status_max,
            "since": since,
            "until": until,
            "key_id": key_id,
        },
        "events": [asdict(e) for e in events],
    }


@router.get("/export-config", response_class=PlainTextResponse)
def export_config() -> PlainTextResponse:
    """Return the active configuration as YAML (secrets redacted).

    The ``ConfigManager.save()`` redaction list is reused: inline
    backend credentials and the bootstrap key never leave the process.
    Operators can commit the output to a config repo or feed it back
    into ``POST /admin/v1/import-config`` on a peer node.
    """
    cfg_dict = container.config_manager.config.model_dump(mode="python")
    redacted = container.config_manager.redact(cfg_dict, mask=False)
    body = yaml.safe_dump(redacted, sort_keys=False)
    headers = {"content-disposition": "attachment; filename=egg-config.yaml"}
    return PlainTextResponse(
        content=body, media_type="application/yaml; charset=utf-8", headers=headers
    )


@router.post("/import-config")
def import_config(request: Request, payload: dict[str, object] | None = Body(default=None)):
    """Replace the active configuration from a YAML or JSON body.

    Accepts ``application/json`` (Pydantic-style dict) or
    ``application/yaml`` (raw text). Validation runs before the
    container swap, so a bad payload leaves the running service on
    its previous config.
    """
    # We accept ``application/json`` directly (FastAPI populates
    # ``payload`` from the body). For YAML, operators can pipe
    # ``yaml.safe_load`` client-side or go through /admin/ui/config.
    if payload is None:
        raise AppError(
            "invalid_parameter",
            "Request body is empty — send JSON with the new config.",
            {"hint": "POST application/json with the config dict"},
            status_code=400,
        )
    try:
        cfg = AppConfig.model_validate(payload)
    except Exception as exc:
        raise AppError(
            "invalid_parameter",
            f"Configuration rejected: {exc}",
            {"scope": "import"},
            status_code=400,
        ) from exc
    container.reload(cfg)
    return {"status": "imported"}


@router.get("/openapi.json")
def admin_openapi_json(request: Request) -> dict[str, object]:
    """Return the full OpenAPI schema, including ``/admin/*`` paths.

    The public ``/v1/openapi.json`` filters admin paths out so anonymous
    callers cannot fingerprint the operator surface; admins with a valid
    key get the unfiltered view here for debugging and integration work.
    """
    return request.app.openapi()


@router.get("/storage/stats")
def storage_stats() -> dict[str, object]:
    """Row counts, on-disk size, schema version and last purge snapshot.

    Intended for capacity planning and retention sanity checks. No secret
    material is returned — only structural counters and aggregate bytes.
    """
    stats = container.store.storage_stats()
    cfg = container.config_manager.config.storage
    # Sprint 10 cleanup: read the purge snapshot off the container
    # instead of reaching back into app.main.
    stats["last_purge"] = dict(container.last_purge_state)
    stats["retention_days"] = cfg.usage_events_retention_days
    stats["purge_interval_seconds"] = cfg.purge_interval_seconds
    return stats
