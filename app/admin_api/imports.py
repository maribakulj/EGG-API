"""Admin REST endpoints for batch importers.

Sprint 22 shipped OAI-PMH / Dublin Core.
Sprint 24 adds OAI-PMH / LIDO and flat-file LIDO by discriminating
on :data:`app.importers.SUPPORTED_KINDS`. The Pydantic payload
validates the kind on the way in so storage never sees an unknown
value; the per-kind dispatch then lives in :mod:`app.importers`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import require_admin_key
from app.dependencies import container
from app.errors import AppError
from app.importers import OAIPMH_KINDS, run_import
from app.importers.oaipmh import identify as oai_identify

router = APIRouter(
    prefix="/admin/v1/imports",
    tags=["admin", "imports"],
    dependencies=[Depends(require_admin_key)],
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateImportSourceRequest(_StrictModel):
    label: str = Field(..., min_length=1, max_length=128)
    # ``url`` holds the OAI-PMH base URL for the oaipmh_* kinds and the
    # absolute filesystem path for every ``*_file`` kind. The dispatcher
    # in :mod:`app.importers` interprets it per kind.
    kind: Literal[
        "oaipmh",
        "oaipmh_lido",
        "oaipmh_marcxml",
        "lido_file",
        "marc_file",
        "marcxml_file",
        "csv_file",
    ] = "oaipmh"
    url: str = Field(..., min_length=1, max_length=2048)
    # For OAI-PMH kinds this is the metadataPrefix; for MARC flat files
    # it carries the flavor (``marc21`` or ``unimarc``); for CSV it is
    # ignored. Empty string means "use the kind's default".
    metadata_prefix: str = Field(default="oai_dc", max_length=64)
    set_spec: str | None = Field(default=None, max_length=256)
    schema_profile: Literal["library", "museum", "archive", "custom"] = "library"


class ImportSourceResponse(_StrictModel):
    id: int
    label: str
    kind: str
    url: str | None
    metadata_prefix: str | None
    set_spec: str | None
    schema_profile: str
    created_at: str
    last_run_at: str | None = None


class ImportRunResponse(_StrictModel):
    id: int
    source_id: int
    started_at: str
    ended_at: str | None
    status: str
    records_ingested: int
    records_failed: int
    error_message: str | None


class RunResult(_StrictModel):
    run_id: int
    status: str
    records_ingested: int
    records_failed: int
    error: str | None = None


def _serialize_source(src: Any) -> ImportSourceResponse:
    return ImportSourceResponse(**asdict(src))


@router.get("", response_model=list[ImportSourceResponse])
def list_sources() -> list[ImportSourceResponse]:
    return [_serialize_source(s) for s in container.store.list_import_sources()]


@router.post("", response_model=ImportSourceResponse, status_code=201)
def create_source(payload: CreateImportSourceRequest) -> ImportSourceResponse:
    src = container.store.add_import_source(
        label=payload.label,
        kind=payload.kind,
        url=payload.url,
        metadata_prefix=payload.metadata_prefix,
        set_spec=payload.set_spec,
        schema_profile=payload.schema_profile,
    )
    return _serialize_source(src)


@router.get("/{source_id}", response_model=ImportSourceResponse)
def get_source(source_id: int) -> ImportSourceResponse:
    src = container.store.get_import_source(source_id)
    if src is None:
        raise AppError(
            "not_found",
            f"Unknown import source: {source_id}",
            {"source_id": source_id},
            status_code=404,
        )
    return _serialize_source(src)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int) -> None:
    if not container.store.delete_import_source(source_id):
        raise AppError(
            "not_found",
            f"Unknown import source: {source_id}",
            {"source_id": source_id},
            status_code=404,
        )


@router.get("/{source_id}/runs", response_model=list[ImportRunResponse])
def list_runs(source_id: int, limit: int = 20) -> list[ImportRunResponse]:
    src = container.store.get_import_source(source_id)
    if src is None:
        raise AppError(
            "not_found",
            f"Unknown import source: {source_id}",
            {"source_id": source_id},
            status_code=404,
        )
    runs = container.store.list_import_runs(source_id, limit=limit)
    return [ImportRunResponse(**asdict(r)) for r in runs]


@router.post("/{source_id}/identify")
def identify_endpoint(source_id: int) -> dict[str, str]:
    """Ping the OAI-PMH endpoint without ingesting anything.

    Available for both ``oaipmh`` and ``oaipmh_lido`` since both share
    the same protocol envelope; flat-file LIDO sources return 400.
    """
    src = container.store.get_import_source(source_id)
    if src is None or src.kind not in OAIPMH_KINDS or not src.url:
        raise AppError(
            "invalid_parameter",
            "Only configured OAI-PMH sources can be identified.",
            {"source_id": source_id},
            status_code=400,
        )
    return oai_identify(src.url)


@router.post("/{source_id}/run", response_model=RunResult)
def run_source(source_id: int) -> RunResult:
    """Trigger a synchronous harvest + bulk index.

    Synchronous by design in S22 so the UI can show immediate
    feedback. S27 wires the scheduler loop on top.
    """
    src = container.store.get_import_source(source_id)
    if src is None:
        raise AppError(
            "not_found",
            f"Unknown import source: {source_id}",
            {"source_id": source_id},
            status_code=404,
        )

    run_id = container.store.start_import_run(source_id)
    try:
        result = run_import(src, bulk_index=container.adapter.bulk_index)
    except ValueError as exc:
        container.store.finish_import_run(
            run_id,
            status="failed",
            records_ingested=0,
            records_failed=0,
            error_message=str(exc),
        )
        raise AppError(
            "invalid_parameter",
            str(exc),
            {"kind": src.kind},
            status_code=400,
        ) from exc
    except Exception as exc:
        container.store.finish_import_run(
            run_id,
            status="failed",
            records_ingested=0,
            records_failed=0,
            error_message=str(exc),
        )
        raise

    status = "failed" if result.error else "succeeded"
    container.store.finish_import_run(
        run_id,
        status=status,
        records_ingested=result.ingested,
        records_failed=result.failed,
        error_message=result.error,
    )
    return RunResult(
        run_id=run_id,
        status=status,
        records_ingested=result.ingested,
        records_failed=result.failed,
        error=result.error,
    )
