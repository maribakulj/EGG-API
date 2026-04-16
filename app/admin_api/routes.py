from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

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
    """Return the current configuration (secrets redacted)."""
    return container.config_manager.config.model_dump(mode="python")


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


@router.get("/status")
def status() -> dict[str, object]:
    """Aggregate backend + mapping health for operator dashboards."""
    cfg = container.config_manager.config
    probe_doc = {"id": "probe", "type": "record"}
    mapping = container.mapping_health.classify(cfg.mapping, probe_doc)
    if any(v == "missing" for v in mapping.values()):
        raise AppError("configuration_error", "Mapping has missing required/recommended sources", {"mapping": mapping}, 500)
    return {"status": "ok", "sources": container.adapter.list_sources(), "mapping": mapping}
