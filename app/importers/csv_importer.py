"""CSV flat-file importer (Sprint 25).

The lowest common denominator for every SIGB/DAMS the admin wizard
cannot talk to directly: export the catalogue to a spreadsheet,
save as CSV, drop it where the server can read it.

Header-driven by design — the first row names each column, and
those names become keys on the backend document. The mapping
wizard then routes those keys onto the public ``Record`` shape, so
CSV integrates with the Sprint 23 ``library`` / ``museum`` /
``archive`` profiles without any per-importer config.

Multi-valued columns (authors, subjects, classifications) can use
a ``|`` separator inside the cell; the importer splits on it and
trims each value. An empty cell becomes ``""`` (the mapper drops it
later in ``first_non_empty`` / ``url_passthrough`` modes).

We use the stdlib ``csv`` module with dialect sniffing so a
semicolon-separated export from French Excel ingests the same way
as a comma-separated one from LibreOffice.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.errors import AppError

logger = logging.getLogger("egg.importers.csv")


DEFAULT_LIST_SEPARATOR = "|"


@dataclass
class CsvImportResult:
    ingested: int
    failed: int
    error: str | None = None


# Columns that should always be split on the list separator, regardless
# of whether the operator knows about the ``|`` convention. Matches the
# plural field names across the library / museum / archive profiles.
_ALWAYS_LIST = frozenset({"creators", "subject", "subjects", "authors", "identifiers", "languages"})


def _detect_dialect(sample: str) -> csv.Dialect | type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _split_cell(column: str, raw: str, list_separator: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    if column in _ALWAYS_LIST or list_separator in raw:
        parts = [p.strip() for p in raw.split(list_separator) if p.strip()]
        if column in _ALWAYS_LIST:
            return parts
        # Single value with no separator → keep as scalar.
        return parts[0] if len(parts) == 1 else parts
    return raw


def parse_csv_bytes(
    data: bytes, *, list_separator: str = DEFAULT_LIST_SEPARATOR
) -> Iterator[dict[str, Any]]:
    """Yield one backend document per non-empty CSV row.

    The first row is treated as the header. Column names become keys
    on the emitted doc. Rows missing an ``id`` column are skipped —
    nothing downstream can reference them.
    """

    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return
    sample = text[:4096]
    dialect = _detect_dialect(sample)
    reader = csv.reader(text.splitlines(), dialect=dialect)
    try:
        header = next(reader)
    except StopIteration:
        return
    header = [h.strip() for h in header]
    if not any(h.lower() == "id" for h in header):
        raise AppError(
            "backend_unavailable",
            "CSV file must have a column named 'id' (any case).",
            {"scope": "csv", "columns": header},
            status_code=400,
        )
    id_index = next(i for i, h in enumerate(header) if h.lower() == "id")
    for row in reader:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < len(header):
            row = list(row) + [""] * (len(header) - len(row))
        row_id = row[id_index].strip()
        if not row_id:
            continue
        doc: dict[str, Any] = {}
        for column, cell in zip(header, row, strict=False):
            if not column:
                continue
            if column.lower() == "id":
                doc["id"] = row_id
                continue
            value = _split_cell(column, cell, list_separator)
            if value == "":
                continue
            doc[column] = value
        if "type" not in doc:
            doc["type"] = "record"
        yield doc


def ingest_csv_file(
    *,
    path: str | Path,
    bulk_index: Any,
    list_separator: str = DEFAULT_LIST_SEPARATOR,
    chunk_size: int = 500,
) -> CsvImportResult:
    target = Path(path)
    if not target.is_file():
        return CsvImportResult(0, 0, error=f"CSV file not found: {target}")

    ingested = 0
    failed = 0
    error: str | None = None
    chunk: list[dict[str, Any]] = []
    try:
        data = target.read_bytes()
        for doc in parse_csv_bytes(data, list_separator=list_separator):
            chunk.append(doc)
            if len(chunk) >= chunk_size:
                added, failed_here = bulk_index(chunk)
                ingested += int(added)
                failed += int(failed_here)
                chunk = []
        if chunk:
            added, failed_here = bulk_index(chunk)
            ingested += int(added)
            failed += int(failed_here)
    except AppError as exc:
        error = exc.message
        logger.exception("csv_ingest_failed", extra={"path": str(target)})
    except OSError as exc:
        error = f"Could not read CSV file: {exc}"
        logger.exception("csv_file_read_failed", extra={"path": str(target)})
    return CsvImportResult(ingested=ingested, failed=failed, error=error)
