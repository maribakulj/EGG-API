"""MARC21 / UNIMARC importer (Sprint 25).

MARC is still the dominant catalogue format in libraries and many
archives. SIGB (Koha, PMB, Orphée, Aleph, Symphony, …) export MARC
as either:

* Binary **ISO 2709** ``.mrc`` files (stream of 24-byte leader +
  directory + fields, terminated by ``0x1D`` record separators);
* **MARCXML** documents (the XML serialisation — one ``<record>`` per
  bibliographic entry), either as flat files or streamed over
  OAI-PMH with ``metadataPrefix=marcxml``.

Two flavors share the same on-disk bytes but differ in tag semantics:

* ``marc21`` — default in English-speaking libraries (LC, Koha in
  many deployments). Title = ``245$a``, main author = ``100$a``,
  publisher = ``260$b`` / ``264$b``, date = ``260$c`` / ``264$c``,
  ISBN = ``020$a``, subject = ``650$a``, note = ``500$a``, record
  id = ``001``.
* ``unimarc`` — common across France, Italy, Portugal and many
  francophone African libraries (PMB, Koha configured for BnF, …).
  Title = ``200$a``, author = ``700$a`` / ``701$a``, publisher =
  ``210$c``, date = ``210$d``, ISBN = ``010$a``, subject = ``606$a``,
  summary = ``330$a``, record id = ``001``.

We hand-roll the ISO 2709 parser rather than pulling in ``pymarc``
to keep the dependency footprint tight — the format is short and
stable (30 years old), so a ~100-line stdlib parser covers every
file we have to read.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from app.errors import AppError

logger = logging.getLogger("egg.importers.marc")


# ISO 2709 delimiters
_FIELD_TERMINATOR = 0x1E
_RECORD_TERMINATOR = 0x1D
_SUBFIELD_DELIMITER = 0x1F

# MARCXML namespace (LoC).
_MARCXML_NS = {"marc": "http://www.loc.gov/MARC21/slim"}


@dataclass
class MarcImportResult:
    ingested: int
    failed: int
    error: str | None = None


@dataclass
class MarcField:
    """One MARC field — either control (tags 001-009) or data (010+)."""

    tag: str
    indicator1: str = " "
    indicator2: str = " "
    # For control fields ``value`` holds the raw text and ``subfields``
    # is empty; for data fields ``subfields`` holds ``[(code, value), …]``.
    value: str = ""
    subfields: list[tuple[str, str]] | None = None


@dataclass
class MarcRecord:
    leader: str
    fields: list[MarcField]

    def get_subfield(self, tag: str, code: str) -> str:
        """Return the first matching subfield value or empty string."""

        for field in self.fields:
            if field.tag == tag and field.subfields:
                for sub_code, value in field.subfields:
                    if sub_code == code and value:
                        return value
        return ""

    def get_subfields(self, tag: str, code: str) -> list[str]:
        out: list[str] = []
        for field in self.fields:
            if field.tag == tag and field.subfields:
                for sub_code, value in field.subfields:
                    if sub_code == code and value:
                        out.append(value)
        return out

    def get_control(self, tag: str) -> str:
        for field in self.fields:
            if field.tag == tag and not field.subfields:
                return field.value
        return ""


# ---------------------------------------------------------------------------
# ISO 2709 low-level parser
# ---------------------------------------------------------------------------


def _split_records(data: bytes) -> Iterator[bytes]:
    """Yield each ISO 2709 record payload (without the ``0x1D`` terminator)."""

    start = 0
    for idx, byte in enumerate(data):
        if byte == _RECORD_TERMINATOR:
            if idx > start:
                yield data[start:idx]
            start = idx + 1
    # Files produced by some SIGB exports forget the trailing 0x1D.
    if start < len(data):
        remainder = data[start:]
        if remainder.strip(b"\x00\n\r "):
            yield remainder


def _decode_text(raw: bytes) -> str:
    """Decode a MARC field with sensible fallbacks.

    ISO 2709 does not mandate an encoding — UTF-8 is now standard
    (MARC-8 was the older option). We try UTF-8 first and fall back
    to latin-1 so even an old ISO-8859-1 export still ingests rather
    than crashing the batch.
    """

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def parse_iso2709_record(record_bytes: bytes) -> MarcRecord:
    """Parse one record payload (24-byte leader + directory + fields)."""

    if len(record_bytes) < 24:
        raise AppError(
            "backend_unavailable",
            "MARC record is shorter than a leader (24 bytes).",
            {"scope": "marc"},
            status_code=502,
        )
    leader = _decode_text(record_bytes[:24])
    try:
        base_address = int(leader[12:17])
    except ValueError as exc:
        raise AppError(
            "backend_unavailable",
            f"MARC record leader has an unparseable base address: {leader[12:17]!r}",
            {"scope": "marc"},
            status_code=502,
        ) from exc

    directory_end = record_bytes.find(_FIELD_TERMINATOR, 24)
    if directory_end == -1 or directory_end > base_address:
        raise AppError(
            "backend_unavailable",
            "MARC record directory is not terminated before the base address.",
            {"scope": "marc"},
            status_code=502,
        )

    directory = record_bytes[24:directory_end]
    if len(directory) % 12 != 0:
        raise AppError(
            "backend_unavailable",
            f"MARC directory length ({len(directory)}) is not a multiple of 12.",
            {"scope": "marc"},
            status_code=502,
        )

    fields: list[MarcField] = []
    for i in range(0, len(directory), 12):
        entry = directory[i : i + 12]
        tag = entry[0:3].decode("ascii", errors="replace")
        try:
            length = int(entry[3:7])
            offset = int(entry[7:12])
        except ValueError:
            continue  # skip malformed directory entry
        start = base_address + offset
        end = start + length
        field_bytes = record_bytes[start:end]
        # Strip the trailing field terminator if present.
        if field_bytes.endswith(bytes([_FIELD_TERMINATOR])):
            field_bytes = field_bytes[:-1]

        if tag.startswith("00"):
            fields.append(MarcField(tag=tag, value=_decode_text(field_bytes)))
            continue

        # Data field: first two bytes are the indicators, followed by
        # subfields delimited by 0x1F.
        if len(field_bytes) < 2:
            fields.append(MarcField(tag=tag, subfields=[]))
            continue
        indicator1 = _decode_text(field_bytes[0:1]) or " "
        indicator2 = _decode_text(field_bytes[1:2]) or " "
        subfields: list[tuple[str, str]] = []
        chunks = field_bytes[2:].split(bytes([_SUBFIELD_DELIMITER]))
        # The first chunk before any 0x1F is discarded (spec says the
        # indicators are followed directly by a subfield delimiter).
        for chunk in chunks[1:]:
            if not chunk:
                continue
            code = _decode_text(chunk[0:1])
            value = _decode_text(chunk[1:])
            subfields.append((code, value))
        fields.append(
            MarcField(
                tag=tag,
                indicator1=indicator1,
                indicator2=indicator2,
                subfields=subfields,
            )
        )
    return MarcRecord(leader=leader, fields=fields)


def iter_iso2709_records(data: bytes) -> Iterator[MarcRecord]:
    """Yield every MARC record parsed from a binary ``.mrc`` blob."""

    for chunk in _split_records(data):
        try:
            yield parse_iso2709_record(chunk)
        except AppError:
            logger.exception("marc_record_parse_failed")
            continue


# ---------------------------------------------------------------------------
# MARCXML
# ---------------------------------------------------------------------------


def _marcxml_record_to_marc_record(record_el: ET.Element) -> MarcRecord:
    leader_el = record_el.find("marc:leader", _MARCXML_NS)
    leader = (leader_el.text or "").strip() if leader_el is not None else " " * 24
    fields: list[MarcField] = []
    for control in record_el.findall("marc:controlfield", _MARCXML_NS):
        tag = (control.get("tag") or "").strip()
        if tag:
            fields.append(MarcField(tag=tag, value=(control.text or "").strip()))
    for data in record_el.findall("marc:datafield", _MARCXML_NS):
        tag = (data.get("tag") or "").strip()
        if not tag:
            continue
        indicator1 = (data.get("ind1") or " ")[:1] or " "
        indicator2 = (data.get("ind2") or " ")[:1] or " "
        subfields: list[tuple[str, str]] = []
        for sub in data.findall("marc:subfield", _MARCXML_NS):
            code = (sub.get("code") or "").strip()
            value = (sub.text or "").strip()
            if code:
                subfields.append((code, value))
        fields.append(
            MarcField(
                tag=tag,
                indicator1=indicator1,
                indicator2=indicator2,
                subfields=subfields,
            )
        )
    return MarcRecord(leader=leader, fields=fields)


def iter_marcxml_records(data: bytes) -> Iterator[MarcRecord]:
    """Parse a MARCXML ``<collection>`` or bare ``<record>`` blob."""

    try:
        root = ET.fromstring(data)  # noqa: S314 — admin-configured input
    except ET.ParseError as exc:
        raise AppError(
            "backend_unavailable",
            f"MARCXML file is not valid XML: {exc}",
            {"scope": "marc"},
            status_code=502,
        ) from exc
    if root.tag == "{http://www.loc.gov/MARC21/slim}record":
        yield _marcxml_record_to_marc_record(root)
        return
    for record_el in root.iterfind(".//marc:record", _MARCXML_NS):
        yield _marcxml_record_to_marc_record(record_el)


# ---------------------------------------------------------------------------
# Record → backend-doc mapping
# ---------------------------------------------------------------------------


def _marc21_doc(record: MarcRecord) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    record_id = record.get_control("001")
    if not record_id:
        return {}
    doc["id"] = record_id
    doc["type"] = "bibliographic"

    title_a = record.get_subfield("245", "a")
    title_b = record.get_subfield("245", "b")
    if title_a or title_b:
        doc["title"] = " ".join(p for p in (title_a, title_b) if p).strip(" /:")

    main_author = record.get_subfield("100", "a")
    added_authors = record.get_subfields("700", "a")
    creators = [a for a in [main_author, *added_authors] if a]
    if creators:
        doc["creators"] = creators

    publisher = record.get_subfield("260", "b") or record.get_subfield("264", "b")
    if publisher:
        doc["publisher"] = publisher.rstrip(" ,;:")
    date = record.get_subfield("260", "c") or record.get_subfield("264", "c")
    if date:
        doc["date"] = date.strip(" .,[]")

    isbn = record.get_subfield("020", "a")
    if isbn:
        doc["isbn"] = isbn.split()[0]  # some records append "(pbk)" notes

    subjects = record.get_subfields("650", "a")
    if subjects:
        doc["subject"] = [s.rstrip(".") for s in subjects]

    description_parts = record.get_subfields("520", "a") + record.get_subfields("500", "a")
    description_parts = [p for p in description_parts if p]
    if description_parts:
        doc["description"] = "\n\n".join(description_parts)

    language = record.get_subfield("041", "a") or record.leader[35:38].strip()
    if language:
        doc["language"] = language
    return doc


def _unimarc_doc(record: MarcRecord) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    record_id = record.get_control("001")
    if not record_id:
        return {}
    doc["id"] = record_id
    doc["type"] = "bibliographic"

    title_a = record.get_subfield("200", "a")
    title_e = record.get_subfield("200", "e")
    if title_a or title_e:
        doc["title"] = " ".join(p for p in (title_a, title_e) if p).strip(" /:")

    main = record.get_subfield("700", "a")
    added = record.get_subfields("701", "a") + record.get_subfields("702", "a")
    creators = [a for a in [main, *added] if a]
    if creators:
        doc["creators"] = creators

    publisher = record.get_subfield("210", "c")
    if publisher:
        doc["publisher"] = publisher.rstrip(" ,;:")
    date = record.get_subfield("210", "d")
    if date:
        doc["date"] = date.strip(" .,[]")

    isbn = record.get_subfield("010", "a")
    if isbn:
        doc["isbn"] = isbn.split()[0]

    subjects = record.get_subfields("606", "a")
    if subjects:
        doc["subject"] = [s.rstrip(".") for s in subjects]

    description = record.get_subfield("330", "a") or record.get_subfield("300", "a")
    if description:
        doc["description"] = description

    language = record.get_subfield("101", "a")
    if language:
        doc["language"] = language
    return doc


def marc_record_to_doc(record: MarcRecord, flavor: str = "marc21") -> dict[str, Any] | None:
    """Map one ``MarcRecord`` to a backend document.

    Returns ``None`` when the record has no control ``001`` — there is
    nothing stable to index it on. ``flavor`` is ``"marc21"`` (default)
    or ``"unimarc"``.
    """

    if flavor == "unimarc":
        doc = _unimarc_doc(record)
    else:
        doc = _marc21_doc(record)
    if not doc or "id" not in doc:
        return None
    return doc


# ---------------------------------------------------------------------------
# Flat-file ingesters
# ---------------------------------------------------------------------------


def ingest_marc_file(
    *,
    path: str | Path,
    bulk_index: Any,
    flavor: str = "marc21",
    chunk_size: int = 500,
) -> MarcImportResult:
    """Stream a binary ``.mrc`` file through ``bulk_index``."""

    target = Path(path)
    if not target.is_file():
        return MarcImportResult(0, 0, error=f"MARC file not found: {target}")

    ingested = 0
    failed = 0
    error: str | None = None
    chunk: list[dict[str, Any]] = []
    try:
        data = target.read_bytes()
        for record in iter_iso2709_records(data):
            doc = marc_record_to_doc(record, flavor)
            if doc is None:
                continue
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
        logger.exception("marc_ingest_failed", extra={"path": str(target)})
    except OSError as exc:
        error = f"Could not read MARC file: {exc}"
        logger.exception("marc_file_read_failed", extra={"path": str(target)})
    return MarcImportResult(ingested=ingested, failed=failed, error=error)


def ingest_marcxml_file(
    *,
    path: str | Path,
    bulk_index: Any,
    flavor: str = "marc21",
    chunk_size: int = 500,
) -> MarcImportResult:
    """Stream a MARCXML file through ``bulk_index``."""

    target = Path(path)
    if not target.is_file():
        return MarcImportResult(0, 0, error=f"MARCXML file not found: {target}")

    ingested = 0
    failed = 0
    error: str | None = None
    chunk: list[dict[str, Any]] = []
    try:
        data = target.read_bytes()
        for record in iter_marcxml_records(data):
            doc = marc_record_to_doc(record, flavor)
            if doc is None:
                continue
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
        logger.exception("marcxml_ingest_failed", extra={"path": str(target)})
    except OSError as exc:
        error = f"Could not read MARCXML file: {exc}"
        logger.exception("marcxml_file_read_failed", extra={"path": str(target)})
    return MarcImportResult(ingested=ingested, failed=failed, error=error)


# ---------------------------------------------------------------------------
# OAI-PMH hook
# ---------------------------------------------------------------------------


def oai_record_parser_for_flavor(flavor: str = "marc21"):
    """Return a record parser compatible with ``oaipmh.iter_records``.

    Closes over ``flavor`` so OAI-PMH harvests of MARCXML correctly
    interpret the tag semantics (``marc21`` default, ``unimarc`` for
    French / BnF deployments).
    """

    def _parse(header: ET.Element, metadata: ET.Element | None) -> dict[str, Any] | None:
        status = header.get("status", "").strip()
        if status == "deleted" or metadata is None:
            return None
        record_el = metadata.find("marc:record", _MARCXML_NS)
        if record_el is None:
            # Some providers forget the collection wrapper; fall back to
            # the first child with MARCXML namespace.
            for child in metadata:
                if child.tag == "{http://www.loc.gov/MARC21/slim}record":
                    record_el = child
                    break
        if record_el is None:
            return None
        record = _marcxml_record_to_marc_record(record_el)
        return marc_record_to_doc(record, flavor)

    return _parse
