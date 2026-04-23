"""LIDO v1.0 importer (Sprint 24).

LIDO (Lightweight Information Describing Objects) is the XML standard
museum collection systems (DAMS such as Micromusée, Axiell, TMS, Mobydoc)
export when they need to feed an aggregator like the Art Data Model or
Europeana. The schema is rooted at ``http://www.lido-schema.org``.

Two import shapes are supported in this sprint:

* **LIDO over OAI-PMH** — the operator points EGG at an OAI-PMH endpoint
  that advertises ``metadataPrefix=lido``. We reuse the OAI-PMH
  envelope + resumption-token plumbing from :mod:`app.importers.oaipmh`
  and plug this module's per-record mapper into the iterator.

* **Flat LIDO XML file** — the operator has a one-shot dump on disk
  (typical of on-premise collection systems that refuse to expose OAI).
  The file may contain either a single ``<lido:lido>`` element, a
  ``<lido:lidoWrap>`` wrapping many, or an arbitrary root that has
  ``<lido:lido>`` descendants. All three layouts are handled.

The mapper targets the *museum* public schema profile (Sprint 23): it
emits ``museum.inventory_number``, ``museum.artist``, ``museum.medium``,
``museum.dimensions``, ``museum.acquisition_date``,
``museum.current_location`` and ``links.iiif_manifest`` as **flat keys
on the backend document** — the admin wizard's ``museum`` profile maps
those directly into the nested ``Record.museum`` sub-block, so both
importers and manual configurations share a single ingest shape.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from app.errors import AppError

logger = logging.getLogger("egg.importers.lido")


_NS = {
    "lido": "http://www.lido-schema.org",
    "oai": "http://www.openarchives.org/OAI/2.0/",
}

# LIDO attribute-namespace used on ``lido:type="inventory number"`` etc.
_LIDO_TYPE_ATTR = "{http://www.lido-schema.org}type"


@dataclass
class LidoFileResult:
    ingested: int
    failed: int
    error: str | None = None


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _first_text(parent: ET.Element, path: str) -> str:
    """Return the first non-empty text matched by ``path`` or ``""``."""

    for node in parent.findall(path, _NS):
        value = (node.text or "").strip()
        if value:
            return value
    return ""


def _all_texts(parent: ET.Element, path: str) -> list[str]:
    return [
        (node.text or "").strip()
        for node in parent.findall(path, _NS)
        if node.text and node.text.strip()
    ]


def _inventory_number(lido: ET.Element) -> str:
    """Return the best inventory number candidate.

    LIDO records can declare several ``<lido:workID>`` entries (one per
    numbering system). We prefer the one marked ``lido:type='inventory
    number'`` / ``'accession number'`` and fall back to the first
    ``workID`` so we always emit *something* when the DAMS exposes one.
    """

    preferred_types = {
        "inventory number",
        "inventory_number",
        "accession number",
        "accession_number",
    }
    first_any = ""
    for work_id in lido.iterfind(
        "lido:descriptiveMetadata/lido:objectIdentificationWrap/"
        "lido:repositoryWrap/lido:repositorySet/lido:workID",
        _NS,
    ):
        value = (work_id.text or "").strip()
        if not value:
            continue
        if not first_any:
            first_any = value
        attr_type = (work_id.get(_LIDO_TYPE_ATTR) or "").strip().lower()
        if attr_type in preferred_types:
            return value
    return first_any


def _production_and_acquisition(lido: ET.Element) -> tuple[str, str, str]:
    """Extract (artist, production_date, acquisition_date) from events."""

    artist = ""
    production_date = ""
    acquisition_date = ""
    for event in lido.iterfind(
        "lido:descriptiveMetadata/lido:eventWrap/lido:eventSet/lido:event",
        _NS,
    ):
        event_type = _first_text(event, "lido:eventType/lido:term").lower()
        display_date = _first_text(event, "lido:eventDate/lido:displayDate")
        if event_type in {"production", "creation", "création"}:
            if not artist:
                actor_names = _all_texts(
                    event,
                    "lido:eventActor/lido:actorInRole/lido:actor/"
                    "lido:nameActorSet/lido:appellationValue",
                )
                if actor_names:
                    artist = actor_names[0]
            if not production_date and display_date:
                production_date = display_date
        elif event_type in {"acquisition", "acquired", "accession"}:
            if not acquisition_date and display_date:
                acquisition_date = display_date
    return artist, production_date, acquisition_date


def _iiif_and_thumbnail(lido: ET.Element) -> tuple[str, str]:
    """Pick IIIF manifest + thumbnail URLs from the admin metadata."""

    iiif = ""
    thumbnail = ""
    for repr_el in lido.iterfind(
        "lido:administrativeMetadata/lido:resourceWrap/lido:resourceSet/"
        "lido:resourceRepresentation",
        _NS,
    ):
        link = _first_text(repr_el, "lido:linkResource")
        if not link:
            continue
        kind = (repr_el.get(_LIDO_TYPE_ATTR) or "").strip().lower()
        if not iiif and ("iiif" in kind or link.lower().rstrip("/").endswith("/manifest")):
            iiif = link
        elif not thumbnail and kind in {"thumbnail", "image_thumb", "preview"}:
            thumbnail = link
    return iiif, thumbnail


def lido_element_to_doc(lido: ET.Element) -> dict[str, Any] | None:
    """Turn a single ``<lido:lido>`` element into a backend document.

    Returns ``None`` when the record has no usable identifier (malformed
    upstream) so the caller can skip it without exploding the batch.
    The output key names match what the Sprint 23 ``museum`` schema
    profile expects on the input side of the mapper — callers do not
    need to know about the public ``Record`` shape.
    """

    record_id = _first_text(lido, "lido:lidoRecID")
    if not record_id:
        record_id = _first_text(
            lido,
            "lido:administrativeMetadata/lido:recordWrap/lido:recordID",
        )
    if not record_id:
        return None

    work_type = _first_text(
        lido,
        "lido:descriptiveMetadata/lido:objectClassificationWrap/"
        "lido:objectWorkTypeWrap/lido:objectWorkType/lido:term",
    )

    titles = _all_texts(
        lido,
        "lido:descriptiveMetadata/lido:objectIdentificationWrap/"
        "lido:titleWrap/lido:titleSet/lido:appellationValue",
    )
    descriptions = _all_texts(
        lido,
        "lido:descriptiveMetadata/lido:objectIdentificationWrap/"
        "lido:objectDescriptionWrap/lido:objectDescriptionSet/"
        "lido:descriptiveNoteValue",
    )
    medium = _first_text(
        lido,
        "lido:descriptiveMetadata/lido:objectIdentificationWrap/"
        "lido:objectMaterialsTechWrap/lido:objectMaterialsTechSet/"
        "lido:materialsTech/lido:termMaterialsTech/lido:term",
    )
    dimensions = _first_text(
        lido,
        "lido:descriptiveMetadata/lido:objectIdentificationWrap/"
        "lido:objectMeasurementsWrap/lido:objectMeasurementsSet/"
        "lido:displayObjectMeasurements",
    )
    current_location = _first_text(
        lido,
        "lido:descriptiveMetadata/lido:objectIdentificationWrap/"
        "lido:repositoryWrap/lido:repositorySet/lido:repositoryName/"
        "lido:legalBodyName/lido:appellationValue",
    )
    inv_no = _inventory_number(lido)
    artist, production_date, acquisition_date = _production_and_acquisition(lido)
    iiif, thumbnail = _iiif_and_thumbnail(lido)

    doc: dict[str, Any] = {"id": record_id, "type": work_type or "object"}
    if titles:
        doc["title"] = titles[0]
    if descriptions:
        doc["description"] = "\n\n".join(descriptions)
    if artist:
        doc["creators"] = [artist]
        doc["artist"] = artist
    if inv_no:
        doc["inventory_number"] = inv_no
    if medium:
        doc["medium"] = medium
    if dimensions:
        doc["dimensions"] = dimensions
    if acquisition_date:
        doc["acquisition_date"] = acquisition_date
    if production_date and "date" not in doc:
        doc["date"] = production_date
    if current_location:
        doc["current_location"] = current_location
    if iiif:
        doc["iiif_manifest"] = iiif
    if thumbnail:
        doc["thumbnail"] = thumbnail
    return doc


def iter_lido_elements(root: ET.Element) -> Iterator[ET.Element]:
    """Yield every ``<lido:lido>`` element reachable from ``root``."""

    if root.tag == "{http://www.lido-schema.org}lido":
        yield root
        return
    yield from root.iterfind(".//lido:lido", _NS)


def parse_lido_bytes(data: bytes) -> Iterator[dict[str, Any]]:
    """Parse an in-memory LIDO XML payload and yield backend docs.

    Accepts a single ``<lido:lido>`` root, a ``<lido:lidoWrap>`` wrapper
    with many children, or any XML tree that has ``<lido:lido>``
    descendants (commonly seen in bespoke DAMS exports). Raises
    :class:`~app.errors.AppError` with scope ``"lido"`` when the bytes
    are not well-formed XML — the caller records that on the import run.
    """

    # ``ET.fromstring`` is not safe against billion-laughs attacks; LIDO
    # dumps come from admin-configured sources (DAMS export, local file
    # upload) so we accept the risk and document it explicitly.
    try:
        root = ET.fromstring(data)  # noqa: S314
    except ET.ParseError as exc:
        raise AppError(
            "backend_unavailable",
            f"LIDO file is not valid XML: {exc}",
            {"scope": "lido"},
            status_code=502,
        ) from exc
    for lido_el in iter_lido_elements(root):
        doc = lido_element_to_doc(lido_el)
        if doc is not None:
            yield doc


def ingest_file(
    *,
    path: str | Path,
    bulk_index: Any,
    chunk_size: int = 500,
) -> LidoFileResult:
    """Stream a flat LIDO XML file through ``bulk_index`` in chunks.

    ``path`` must be an absolute filesystem path the server can read
    (we do not accept multipart uploads in this sprint to keep the
    memory / storage story simple on the desktop build — the operator
    drops the file into an accessible folder and points EGG at it).
    """

    target = Path(path)
    if not target.is_file():
        return LidoFileResult(ingested=0, failed=0, error=f"LIDO file not found: {target}")

    ingested = 0
    failed = 0
    error: str | None = None
    chunk: list[dict[str, Any]] = []
    try:
        data = target.read_bytes()
        for doc in parse_lido_bytes(data):
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
        logger.exception("lido_file_ingest_failed", extra={"path": str(target)})
    except OSError as exc:
        error = f"Could not read LIDO file: {exc}"
        logger.exception("lido_file_read_failed", extra={"path": str(target)})
    return LidoFileResult(ingested=ingested, failed=failed, error=error)


def oai_record_to_doc(header: ET.Element, metadata: ET.Element | None) -> dict[str, Any] | None:
    """Parse one OAI-PMH ``<record>`` whose payload is LIDO.

    Mirrors :func:`app.importers.oaipmh.dc_record_to_doc` so the two
    plug interchangeably into :func:`app.importers.oaipmh.iter_records`
    once we pass this parser as the record handler.
    """

    status = header.get("status", "").strip()
    if status == "deleted":
        return None
    if metadata is None:
        return None
    lido_wrap = metadata.find("lido:lidoWrap", _NS)
    if lido_wrap is not None:
        lido_el = lido_wrap.find("lido:lido", _NS)
    else:
        lido_el = metadata.find("lido:lido", _NS)
    if lido_el is None:
        # Rare fallback: payload is a bare <lido:lido> (no wrap).
        for child in metadata:
            if child.tag == "{http://www.lido-schema.org}lido":
                lido_el = child
                break
    if lido_el is None:
        return None
    return lido_element_to_doc(lido_el)


def docs_from_oai(
    records: Iterable[tuple[ET.Element, ET.Element | None]],
) -> Iterator[dict[str, Any]]:
    """Adapter for callers that have already walked the OAI envelope."""

    for header, metadata in records:
        doc = oai_record_to_doc(header, metadata)
        if doc is not None:
            yield doc
