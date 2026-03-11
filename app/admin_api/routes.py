from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from app.auth.dependencies import require_admin_key
from app.config.models import AppConfig
from app.dependencies import container
from app.errors import AppError

router = APIRouter(prefix="/admin/v1", tags=["admin"], dependencies=[Depends(require_admin_key)])


@router.post("/setup/detect")
def detect() -> dict[str, object]:
    return container.adapter.detect()


@router.post("/setup/scan-fields")
def scan_fields() -> dict[str, object]:
    return container.adapter.scan_fields()


@router.post("/setup/create-config")
def create_config(payload: dict[str, object] = Body(default_factory=dict)) -> dict[str, object]:
    cfg = AppConfig.model_validate(payload or {})
    container.reload(cfg)
    return {"status": "created"}


@router.get("/config")
def get_config() -> dict[str, object]:
    return {
        "config_path": str(container.config_manager.path),
        "state_db_path": str(container.store.db_path),
        "config": container.config_manager.config.model_dump(mode="python"),
    }


@router.put("/config")
def put_config(payload: dict[str, object]) -> dict[str, object]:
    cfg = AppConfig.model_validate(payload)
    container.reload(cfg)
    return {"status": "updated"}


@router.post("/config/validate")
def validate_config(payload: dict[str, object]) -> dict[str, object]:
    ok, error = container.config_manager.validate_data(payload)
    return {"valid": ok, "error": error}


@router.post("/test-query")
def test_query(request: Request) -> dict[str, object]:
    nq = container.policy.parse(request)
    return {"translated": container.adapter.translate_query(nq)}


@router.get("/status")
def status() -> dict[str, object]:
    cfg = container.config_manager.config
    probe_doc = {"id": "probe", "type": "record"}
    mapping = container.mapping_health.classify(cfg.mapping, probe_doc)
    required_missing = [
        field
        for field, state in mapping.items()
        if state == "missing" and cfg.mapping.get(field) and cfg.mapping[field].criticality == "required"
    ]
    warnings = [
        field
        for field, state in mapping.items()
        if state == "missing" and cfg.mapping.get(field) and cfg.mapping[field].criticality == "recommended"
    ]
    if required_missing:
        raise AppError("configuration_error", "Mapping has missing required sources", {"mapping": mapping, "required_missing": required_missing}, 500)
    return {
        "status": "ok",
        "sources": container.adapter.list_sources(),
        "mapping": mapping,
        "usage": container.store.usage_summary(),
        "state_db_path": str(container.store.db_path),
        "warnings": warnings,
    }
