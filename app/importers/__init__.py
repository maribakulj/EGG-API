"""app.importers — hooks for batch ingestion into the search backend.

Each importer yields ``dict`` documents that the active
:class:`~app.adapters.base.BackendAdapter`'s ``bulk_index()`` then
ships to the search engine. Keeping importers dict-shaped rather
than ``Record``-shaped leaves every backend-specific pre-processing
(timestamp normalisation, nested-field flattening, …) at the
adapter boundary.

Sprint 22 ships OAI-PMH / Dublin Core.
Sprint 24 adds LIDO (museum DAMS) over OAI-PMH **and** flat-file upload.
Sprint 25 adds MARC21 / UNIMARC (ISO 2709 binary and MARCXML, over
OAI-PMH or flat files) plus CSV flat-file.
Sprint 26 adds EAD (archive finding aids).

The :func:`run_import` function in this module is the single
dispatcher both the admin REST API (``/admin/v1/imports/{id}/run``)
and the admin UI call to execute an import, so the mapping of
``ImportSource.kind`` → concrete ingest call lives in exactly one
place.

Scope freeze (see docs/adr-002-compiler-separation.md)
------------------------------------------------------
These are **lightweight built-in importers**. They extract flat
fields for a straight bulk-index into the backend; they deliberately
do **not** build a semantic model (events, agents with roles,
normalised patrimonial dates, Linked Art projection, CIDOC CRM
alignment). Advanced format transformation is delegated to the
external document compiler described in ADR 002.

For contributors: if you are about to add a new field mapping to
LIDO / EAD / MARC here — stop and ask whether it belongs in the
compiler instead. The built-in importers are in maintenance: bug
fixes welcome, new semantic coverage is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.importers.csv_importer import ingest_csv_file
from app.importers.ead import (
    ingest_file as ead_ingest_file,
    oai_record_to_docs as ead_oai_record_to_docs,
)
from app.importers.lido import (
    ingest_file as lido_ingest_file,
    oai_record_to_doc as lido_oai_record_to_doc,
)
from app.importers.marc import (
    ingest_marc_file,
    ingest_marcxml_file,
    oai_record_parser_for_flavor,
)
from app.importers.oaipmh import ingest as oai_ingest

# Every ``kind`` the system knows about. The admin REST schema
# validates the incoming payload against this set; the UI template
# renders the matching label for each entry.
SUPPORTED_KINDS: tuple[str, ...] = (
    "oaipmh",
    "oaipmh_lido",
    "oaipmh_marcxml",
    "oaipmh_ead",
    "lido_file",
    "marc_file",
    "marcxml_file",
    "csv_file",
    "ead_file",
)

# Sub-set that supports the OAI-PMH ``Identify`` verb. The admin API
# and UI guard against calling ``/identify`` on flat-file kinds.
OAIPMH_KINDS: frozenset[str] = frozenset({"oaipmh", "oaipmh_lido", "oaipmh_marcxml", "oaipmh_ead"})

# MARC flavors supported by :mod:`app.importers.marc`. Stored on the
# ``metadata_prefix`` column for MARC kinds.
SUPPORTED_MARC_FLAVORS: frozenset[str] = frozenset({"marc21", "unimarc"})


@dataclass
class ImportDispatchResult:
    ingested: int
    failed: int
    error: str | None = None


def _marc_flavor(source: Any) -> str:
    raw = (source.metadata_prefix or "").strip().lower()
    if raw in SUPPORTED_MARC_FLAVORS:
        return raw
    return "marc21"


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
    if kind == "oaipmh_marcxml":
        if not url:
            return ImportDispatchResult(0, 0, "OAI-PMH (MARCXML) source has no URL")
        # ``metadata_prefix`` on the row carries the flavor for MARC
        # kinds, so default the OAI prefix to the standard value.
        flavor = _marc_flavor(source)
        result = oai_ingest(
            url=url,
            metadata_prefix="marcxml",
            set_spec=source.set_spec,
            bulk_index=bulk_index,
            record_parser=oai_record_parser_for_flavor(flavor),
        )
        return ImportDispatchResult(result.ingested, result.failed, result.error)
    if kind == "lido_file":
        if not url:
            return ImportDispatchResult(0, 0, "LIDO file source has no path")
        file_result = lido_ingest_file(path=url, bulk_index=bulk_index)
        return ImportDispatchResult(file_result.ingested, file_result.failed, file_result.error)
    if kind == "marc_file":
        if not url:
            return ImportDispatchResult(0, 0, "MARC file source has no path")
        marc_result = ingest_marc_file(path=url, flavor=_marc_flavor(source), bulk_index=bulk_index)
        return ImportDispatchResult(marc_result.ingested, marc_result.failed, marc_result.error)
    if kind == "marcxml_file":
        if not url:
            return ImportDispatchResult(0, 0, "MARCXML file source has no path")
        marcxml_result = ingest_marcxml_file(
            path=url, flavor=_marc_flavor(source), bulk_index=bulk_index
        )
        return ImportDispatchResult(
            marcxml_result.ingested, marcxml_result.failed, marcxml_result.error
        )
    if kind == "csv_file":
        if not url:
            return ImportDispatchResult(0, 0, "CSV file source has no path")
        csv_result = ingest_csv_file(path=url, bulk_index=bulk_index)
        return ImportDispatchResult(csv_result.ingested, csv_result.failed, csv_result.error)
    if kind == "ead_file":
        if not url:
            return ImportDispatchResult(0, 0, "EAD file source has no path")
        ead_result = ead_ingest_file(path=url, bulk_index=bulk_index)
        return ImportDispatchResult(ead_result.ingested, ead_result.failed, ead_result.error)
    if kind == "oaipmh_ead":
        if not url:
            return ImportDispatchResult(0, 0, "OAI-PMH (EAD) source has no URL")
        # EAD payloads expand to many docs per OAI record; the OAI
        # iterator accepts a parser that returns a list for this case.
        result = oai_ingest(
            url=url,
            metadata_prefix=source.metadata_prefix or "ead",
            set_spec=source.set_spec,
            bulk_index=bulk_index,
            record_parser=ead_oai_record_to_docs,
        )
        return ImportDispatchResult(result.ingested, result.failed, result.error)
    raise ValueError(f"Unknown import source kind: {kind!r}")
