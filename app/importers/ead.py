"""EAD (Encoded Archival Description) importer (Sprint 26).

EAD is the XML standard finding-aid format every serious archive
software ends up supporting — AtoM, Mnesys, Ligeo, ArchivesSpace,
Calames, PLEADE, Pict'oOpen. Two schema generations are in wide
deployment:

* **EAD 2002** — the original DTD; files carry no XML namespace,
  or sometimes ``urn:isbn:1-931666-22-9`` on the root.
* **EAD3** (2015+) — the XSD successor; root namespace
  ``http://ead3.archivists.org/schema/``.

The element names overlap almost entirely (``<archdesc>``, ``<did>``,
``<unitid>``, ``<unittitle>``, ``<unitdate>``, ``<origination>``,
``<scopecontent>``, ``<accessrestrict>``, ``<c>`` components). The
parser matches by local name so the same code handles both.

Each finding aid becomes **multiple backend documents**: one for
the ``<archdesc>`` root (the fonds or collection as a whole) and
one for every ``<c>`` / ``<c01>``...``<c12>`` descendant. Each
component carries a ``parent_id`` pointer so clients can rebuild
the hierarchy without the importer having to flatten or denormalise
it.

Like Sprint 24-25, no heavyweight deps: stdlib
``xml.etree.ElementTree`` is enough for EAD's mild structural
needs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from app.errors import AppError

logger = logging.getLogger("egg.importers.ead")


# Local-name matching helpers — EAD 2002 files have no namespace,
# EAD3 uses ``http://ead3.archivists.org/schema/``. Rather than thread
# a namespace map through every ``.find`` call, we pattern-match on
# the element's local name (``{ns}tag`` → ``tag``).


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_local(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent.iter():
        if _local(child.tag) == name:
            return child
    return None


def _direct_children(parent: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in parent:
        if _local(child.tag) == name:
            yield child


def _descendants(parent: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in parent.iter():
        if child is parent:
            continue
        if _local(child.tag) == name:
            yield child


def _text(el: ET.Element | None) -> str:
    """Return the concatenated visible text of an element, trimmed.

    EAD sprinkles mixed content inside elements (``<emph>``, ``<lb/>``,
    ``<title>``, …). ``ET.itertext`` recursively yields text nodes
    so we keep the reading order while dropping the tags.
    """

    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _paragraphs(el: ET.Element | None) -> str:
    """Concatenate ``<p>`` children with blank lines between them."""

    if el is None:
        return ""
    parts: list[str] = []
    paras = list(_direct_children(el, "p"))
    if not paras:
        # Some EAD3 dumps put the text directly in <scopecontent> without
        # wrapping paragraphs — fall back to the full text.
        return _text(el)
    for p in paras:
        value = _text(p)
        if value:
            parts.append(value)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Doc assembly
# ---------------------------------------------------------------------------


_COMPONENT_TAGS = {f"c{str(i).zfill(2)}" for i in range(1, 13)} | {"c"}


@dataclass
class EadImportResult:
    ingested: int
    failed: int
    error: str | None = None


def _best_id(did: ET.Element, fallback: str) -> str:
    """Prefer ``<unitid>`` text; fall back to ``@id`` on the component."""

    unitid = _text(_find_local(did, "unitid"))
    if unitid:
        return unitid
    # EAD 2002 / EAD3 both allow ``id``/``xml:id`` on components.
    for attr in ("id", "{http://www.w3.org/XML/1998/namespace}id"):
        raw = did.get(attr)
        if raw:
            return raw
    return fallback


def _component_to_doc(
    component: ET.Element,
    *,
    parent_id: str | None,
    fallback_id: str,
    level_override: str | None = None,
    repository: str | None = None,
) -> dict[str, Any] | None:
    """Turn one EAD component (``<archdesc>`` or ``<c*>``) into a doc."""

    did = None
    for child in component:
        if _local(child.tag) == "did":
            did = child
            break
    if did is None:
        return None

    record_id = _best_id(did, fallback_id)
    if not record_id:
        return None

    unit_level = level_override or (component.get("level") or "").strip() or "component"

    title = _text(_find_local(did, "unittitle"))
    # Date: prefer @normal, fall back to the text.
    date = ""
    for unitdate in _descendants(did, "unitdate"):
        normal = (unitdate.get("normal") or "").strip()
        if normal:
            date = normal.split("/")[0]
            break
        text_value = _text(unitdate)
        if text_value:
            date = text_value
            break

    origination = _text(_find_local(did, "origination"))
    extent_el = _find_local(did, "extent")
    extent = _text(extent_el) or _text(_find_local(did, "physdesc"))
    repo_text = _text(_find_local(did, "repository")) or (repository or "")

    scope_el = None
    access_el = None
    for child in component:
        if scope_el is None and _local(child.tag) == "scopecontent":
            scope_el = child
        elif access_el is None and _local(child.tag) == "accessrestrict":
            access_el = child
    scope_content = _paragraphs(scope_el)
    access_conditions = _paragraphs(access_el)

    doc: dict[str, Any] = {
        "id": record_id,
        "type": unit_level,
    }
    if title:
        doc["title"] = title
    if origination:
        doc["creators"] = [origination]
    if scope_content:
        doc["description"] = scope_content
    if date:
        doc["date"] = date
    # Archive-profile sub-block sources (flat keys the mapper can
    # route to ``archive.*`` via dotted mapping rules).
    if record_id:
        doc["unit_id"] = record_id
    if unit_level and unit_level != "component":
        doc["unit_level"] = unit_level
    if extent:
        doc["extent"] = extent
    if repo_text:
        doc["repository"] = repo_text
    if scope_content:
        doc["scope_content"] = scope_content
    if access_conditions:
        doc["access_conditions"] = access_conditions
    if parent_id:
        doc["parent_id"] = parent_id
    return doc


def iter_ead_docs(root: ET.Element) -> Iterator[dict[str, Any]]:
    """Yield one backend doc per ``<archdesc>`` + every descendant ``<c*>``.

    EAD root is either:
    * ``<ead>`` with ``<archdesc>`` inside (most common), or
    * a bare ``<archdesc>`` (OAI-PMH metadataPrefix=ead often wraps
      only the archival description), or
    * anything else with ``<archdesc>`` descendants.
    """

    local_root = _local(root.tag)
    if local_root == "ead":
        archdesc = None
        for child in root:
            if _local(child.tag) == "archdesc":
                archdesc = child
                break
        if archdesc is None:
            return
    elif local_root == "archdesc":
        archdesc = root
    else:
        archdesc = _find_local(root, "archdesc")
        if archdesc is None:
            return

    repository = _text(_find_local(archdesc, "repository")) or None
    # Generate a synthetic fallback id for the archdesc if none is
    # available — EAD dumps usually include one, but we never want to
    # skip the top-level record.
    archdesc_doc = _component_to_doc(
        archdesc,
        parent_id=None,
        fallback_id="archdesc-root",
        level_override=(archdesc.get("level") or "fonds"),
        repository=repository,
    )
    if archdesc_doc is None:
        return
    yield archdesc_doc

    # Walk every <c*> descendant, carrying the parent pointer along.
    # EAD 2002 uses numbered c01-c12 (depth-encoded); EAD3 uses bare
    # <c> with real hierarchy.
    def _walk(
        node: ET.Element, parent_id: str, depth_counter: dict[str, int]
    ) -> Iterator[dict[str, Any]]:
        for child in node:
            if _local(child.tag) not in _COMPONENT_TAGS:
                continue
            depth_counter["n"] += 1
            child_doc = _component_to_doc(
                child,
                parent_id=parent_id,
                fallback_id=f"{parent_id}-{depth_counter['n']}",
                repository=repository,
            )
            if child_doc is not None:
                yield child_doc
                yield from _walk(
                    child,
                    parent_id=child_doc["id"],
                    depth_counter=depth_counter,
                )

    dsc = None
    for child in archdesc:
        if _local(child.tag) == "dsc":
            dsc = child
            break
    if dsc is None:
        return
    yield from _walk(dsc, parent_id=archdesc_doc["id"], depth_counter={"n": 0})


# ---------------------------------------------------------------------------
# Flat-file parse + ingest
# ---------------------------------------------------------------------------


def parse_ead_bytes(data: bytes) -> Iterator[dict[str, Any]]:
    """Parse an in-memory EAD XML payload and yield backend docs."""

    try:
        root = ET.fromstring(data)  # noqa: S314 — admin-configured input
    except ET.ParseError as exc:
        raise AppError(
            "backend_unavailable",
            f"EAD file is not valid XML: {exc}",
            {"scope": "ead"},
            status_code=502,
        ) from exc
    yield from iter_ead_docs(root)


def ingest_file(
    *,
    path: str | Path,
    bulk_index: Any,
    chunk_size: int = 500,
) -> EadImportResult:
    """Stream a flat EAD XML file through ``bulk_index`` in chunks."""

    target = Path(path)
    if not target.is_file():
        return EadImportResult(0, 0, error=f"EAD file not found: {target}")

    ingested = 0
    failed = 0
    error: str | None = None
    chunk: list[dict[str, Any]] = []
    try:
        data = target.read_bytes()
        for doc in parse_ead_bytes(data):
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
        logger.exception("ead_ingest_failed", extra={"path": str(target)})
    except OSError as exc:
        error = f"Could not read EAD file: {exc}"
        logger.exception("ead_file_read_failed", extra={"path": str(target)})
    return EadImportResult(ingested=ingested, failed=failed, error=error)


# ---------------------------------------------------------------------------
# OAI-PMH hook
# ---------------------------------------------------------------------------


def oai_record_to_doc(header: ET.Element, metadata: ET.Element | None) -> dict[str, Any] | None:
    """Parse the *first* doc from one OAI-PMH ``<record>`` whose payload is EAD.

    OAI-PMH wraps a single metadata payload per record. For EAD that
    payload is commonly an ``<ead>`` or ``<archdesc>`` describing one
    finding aid. Since each finding aid can produce many docs (the
    root + components), the OAI record parser cannot stream them —
    so we return only the top-level doc here and emit the component
    tree through a dedicated bulk path in the dispatcher when the
    admin wants full hierarchy. This keeps the pluggable
    ``iter_records`` contract (one-record-per-OAI-record) intact.
    """

    status = header.get("status", "").strip()
    if status == "deleted" or metadata is None:
        return None
    # Payload may be <ead> or <archdesc>; find the first one.
    for child in metadata.iter():
        if _local(child.tag) in {"ead", "archdesc"}:
            docs = list(iter_ead_docs(child))
            return docs[0] if docs else None
    return None


def oai_record_to_docs(header: ET.Element, metadata: ET.Element | None) -> list[dict[str, Any]]:
    """Return every doc (root + components) for one OAI record."""

    status = header.get("status", "").strip()
    if status == "deleted" or metadata is None:
        return []
    for child in metadata.iter():
        if _local(child.tag) in {"ead", "archdesc"}:
            return list(iter_ead_docs(child))
    return []
