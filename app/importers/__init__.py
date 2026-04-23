"""app.importers — hooks for batch ingestion into the search backend.

Each importer yields ``dict`` documents that the active
:class:`~app.adapters.base.BackendAdapter`'s ``bulk_index()`` then
ships to the search engine. Keeping importers dict-shaped rather
than ``Record``-shaped leaves every backend-specific pre-processing
(timestamp normalisation, nested-field flattening, …) at the
adapter boundary.

Sprint 22 ships OAI-PMH / Dublin Core.
Sprint 24 adds LIDO (museum DAMS) over OAI-PMH **and** flat-file upload.
Sprint 25-26 add MARC/UNIMARC, CSV/XLSX, EAD.

The :func:`run_import` function in this module is the single
dispatcher both the admin REST API (``/admin/v1/imports/{id}/run``)
and the admin UI call to execute an import, so the mapping of
``ImportSource.kind`` → concrete ingest call lives in exactly one
place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.importers.lido import (
    ingest_file as lido_ingest_file,
    oai_record_to_doc as lido_oai_record_to_doc,
)
from app.importers.oaipmh import ingest as oai_ingest

# Every ``kind`` the system knows about. The admin REST schema
# validates the incoming payload against this set; the UI template
# renders the matching label for each entry.
SUPPORTED_KINDS: tuple[str, ...] = ("oaipmh", "oaipmh_lido", "lido_file")


@dataclass
class ImportDispatchResult:
    ingested: int
    failed: int
    error: str | None = None


def run_import(source: Any, *, bulk_index: Any) -> ImportDispatchResult:
    """Run the importer matching ``source.kind``.

    ``source`` is an ``ImportSource`` row (duck-typed: we only read
    ``.kind``, ``.url``, ``.metadata_prefix``, ``.set_spec``).
    ``bulk_index`` is the callable that ships a list of docs to the
    backend and returns ``(ingested, failed)``.

    Unknown kinds raise ``ValueError`` — the caller is expected to
    have validated the kind before writing it to storage, so hitting
    this branch indicates data corruption, not user input.
    """

    kind = source.kind
    url = source.url
    if kind == "oaipmh":
        if not url:
            return ImportDispatchResult(0, 0, "OAI-PMH source has no URL")
        result = oai_ingest(
            url=url,
            metadata_prefix=source.metadata_prefix or "oai_dc",
            set_spec=source.set_spec,
            bulk_index=bulk_index,
        )
        return ImportDispatchResult(result.ingested, result.failed, result.error)
    if kind == "oaipmh_lido":
        if not url:
            return ImportDispatchResult(0, 0, "OAI-PMH (LIDO) source has no URL")
        result = oai_ingest(
            url=url,
            metadata_prefix=source.metadata_prefix or "lido",
            set_spec=source.set_spec,
            bulk_index=bulk_index,
            record_parser=lido_oai_record_to_doc,
        )
        return ImportDispatchResult(result.ingested, result.failed, result.error)
    if kind == "lido_file":
        if not url:
            return ImportDispatchResult(0, 0, "LIDO file source has no path")
        file_result = lido_ingest_file(path=url, bulk_index=bulk_index)
        return ImportDispatchResult(file_result.ingested, file_result.failed, file_result.error)
    raise ValueError(f"Unknown import source kind: {kind!r}")
