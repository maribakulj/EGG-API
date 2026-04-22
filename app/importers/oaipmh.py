"""Minimal OAI-PMH 2.0 client + Dublin Core mapper (Sprint 22).

OAI-PMH is just HTTP GET with XML replies, so we avoid pulling a
third-party client (sickle drags ``requests``; lxml adds a big C
dep). The stdlib ``xml.etree.ElementTree`` is enough — we only
need ``Identify`` and ``ListRecords``, both well-specified verbs.

The client is an iterator: ``ingest(source, adapter, store)``
streams records from the upstream, chunks them by 500, and hands
each chunk to the adapter's ``bulk_index()`` so the server never
holds the whole response in memory.

Dublin Core mapping targets the public EGG record shape:

    dc:identifier  →  id  (first non-empty)
    dc:type        →  type (fallback "record")
    dc:title       →  title (first non-empty)
    dc:description →  description (concatenated)
    dc:creator     →  creators (list)
    dc:date        →  date (first non-empty)
    dc:subject     →  subject (list)
    dc:language    →  language
    dc:publisher   →  publisher
    dc:rights      →  rights
    dc:identifier  →  iiif_manifest (heuristic: contains /iiif/ or /manifest)

Deleted records (oai:header[@status='deleted']) are skipped in this
sprint — incremental delete propagation is a Sprint 27 concern.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.errors import AppError

logger = logging.getLogger("egg.importers.oaipmh")


_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}


# Number of records per ``bulk_index`` batch. Enough to amortise the
# HTTP round-trip to ES without blowing past the ES default
# ``http.max_content_length`` (100 MB).
DEFAULT_CHUNK_SIZE = 500


@dataclass
class OAIImportResult:
    ingested: int
    failed: int
    error: str | None = None


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _all_texts(parent: ET.Element, tag: str) -> list[str]:
    return [
        (child.text or "").strip()
        for child in parent.findall(tag, _NS)
        if child.text and child.text.strip()
    ]


def _looks_like_iiif_manifest(value: str) -> bool:
    lower = value.lower()
    return "/iiif/" in lower or lower.endswith("/manifest") or "manifest.json" in lower


def dc_record_to_doc(header: ET.Element, metadata: ET.Element | None) -> dict[str, Any] | None:
    """Turn one OAI-PMH ``<record>`` into a backend document.

    Returns ``None`` for deleted records or records without any
    identifier (malformed upstream). The returned ``dict`` is the
    shape the adapter stores natively — no public ``Record``
    validation here; the mapping layer on the read path handles it.
    """
    status = header.get("status", "").strip()
    if status == "deleted":
        return None

    header_identifier = _text(header.find("oai:identifier", _NS))

    doc: dict[str, Any] = {}
    if metadata is not None:
        dc_root = metadata.find("oai_dc:dc", _NS) or metadata.find("dc", _NS)
        if dc_root is None:
            # Payloads sometimes wrap Dublin Core under a different
            # prefix; take the first child with dc: children.
            for child in metadata:
                if any(c.tag.startswith("{" + _NS["dc"]) for c in child):
                    dc_root = child
                    break
        if dc_root is not None:
            identifiers = _all_texts(dc_root, "dc:identifier")
            titles = _all_texts(dc_root, "dc:title")
            descriptions = _all_texts(dc_root, "dc:description")
            creators = _all_texts(dc_root, "dc:creator")
            subjects = _all_texts(dc_root, "dc:subject")
            dates = _all_texts(dc_root, "dc:date")
            types = _all_texts(dc_root, "dc:type")
            languages = _all_texts(dc_root, "dc:language")
            publishers = _all_texts(dc_root, "dc:publisher")
            rights = _all_texts(dc_root, "dc:rights")

            if identifiers:
                doc["id"] = identifiers[0]
                # Extra identifiers kept verbatim for consumers that
                # want the raw list (e.g. DOIs + URLs).
                if len(identifiers) > 1:
                    doc["identifiers"] = identifiers
                # Heuristic for IIIF manifest URL.
                for cand in identifiers:
                    if _looks_like_iiif_manifest(cand):
                        doc["iiif_manifest"] = cand
                        break
            if titles:
                doc["title"] = titles[0]
            if descriptions:
                doc["description"] = "\n\n".join(descriptions)
            if creators:
                doc["creators"] = creators
            if subjects:
                doc["subject"] = subjects
            if dates:
                doc["date"] = dates[0]
            if types:
                doc["type"] = types[0]
            else:
                doc["type"] = "record"
            if languages:
                doc["language"] = languages[0]
            if publishers:
                doc["publisher"] = publishers[0]
            if rights:
                doc["rights"] = rights[0]

    # Fall back to the OAI header identifier when no dc:identifier
    # was present — at least we have something stable to key on.
    if "id" not in doc and header_identifier:
        doc["id"] = header_identifier
    if "type" not in doc:
        doc["type"] = "record"

    if "id" not in doc:
        return None  # unusable, skip
    return doc


def _parse_response(xml_body: bytes) -> ET.Element:
    # Stdlib ``xml.etree`` is not a safe XML parser against billion-laughs
    # attacks; OAI-PMH endpoints are admin-configured and generally trusted,
    # but document the choice so reviewers know we considered it.
    try:
        return ET.fromstring(xml_body)  # noqa: S314
    except ET.ParseError as exc:
        raise AppError(
            "backend_unavailable",
            f"OAI-PMH response is not valid XML: {exc}",
            {"scope": "oaipmh"},
            status_code=502,
        ) from exc


def _detect_oaipmh_error(root: ET.Element) -> None:
    error = root.find("oai:error", _NS)
    if error is not None:
        code = (error.get("code") or "error").strip()
        raise AppError(
            "backend_unavailable",
            f"OAI-PMH server returned an error: {code} — {(error.text or '').strip()}",
            {"scope": "oaipmh", "oai_error": code},
            status_code=502,
        )


def identify(
    url: str, *, client: httpx.Client | None = None, timeout: float = 10.0
) -> dict[str, str]:
    """Call ``?verb=Identify`` and return a summary dict.

    Used by the UI to show *"we reached repo X, last update Y"*
    before the operator commits an import. Raises
    ``AppError("backend_unavailable", …)`` on HTTP / XML / OAI-level
    failure so the caller can display a single clean error.
    """
    close_after = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
        close_after = True
    try:
        try:
            resp = client.get(url, params={"verb": "Identify"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(
                "backend_unavailable",
                f"Could not reach OAI-PMH endpoint: {exc}",
                {"scope": "oaipmh", "url": url},
                status_code=502,
            ) from exc
        root = _parse_response(resp.content)
        _detect_oaipmh_error(root)
        info = root.find("oai:Identify", _NS)
        if info is None:
            raise AppError(
                "backend_unavailable",
                "OAI-PMH response did not contain an Identify block.",
                {"scope": "oaipmh"},
                status_code=502,
            )
        return {
            "repository_name": _text(info.find("oai:repositoryName", _NS)),
            "base_url": _text(info.find("oai:baseURL", _NS)),
            "protocol_version": _text(info.find("oai:protocolVersion", _NS)),
            "earliest_datestamp": _text(info.find("oai:earliestDatestamp", _NS)),
            "granularity": _text(info.find("oai:granularity", _NS)),
        }
    finally:
        if close_after:
            client.close()


def iter_records(
    url: str,
    *,
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
    max_pages: int = 10_000,
) -> Iterator[dict[str, Any]]:
    """Yield one backend document per OAI-PMH record, following
    resumption tokens.

    ``max_pages`` is a safety ceiling: a misbehaving server returning
    an infinite loop of tokens will raise an ``AppError`` rather
    than ingesting forever. Each page stays in memory just long
    enough to parse and yield its records.
    """
    close_after = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
        close_after = True
    try:
        params: dict[str, str] = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
        if set_spec:
            params["set"] = set_spec

        for page_index in range(max_pages):
            try:
                resp = client.get(url, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise AppError(
                    "backend_unavailable",
                    f"OAI-PMH fetch failed on page {page_index + 1}: {exc}",
                    {"scope": "oaipmh", "url": url},
                    status_code=502,
                ) from exc
            root = _parse_response(resp.content)
            _detect_oaipmh_error(root)

            list_records = root.find("oai:ListRecords", _NS)
            if list_records is None:
                return
            for record in list_records.findall("oai:record", _NS):
                header = record.find("oai:header", _NS)
                metadata = record.find("oai:metadata", _NS)
                if header is None:
                    continue
                doc = dc_record_to_doc(header, metadata)
                if doc is not None:
                    yield doc

            token_el = list_records.find("oai:resumptionToken", _NS)
            token = (_text(token_el) or "") if token_el is not None else ""
            if not token:
                return
            # Subsequent requests use only ``verb`` + ``resumptionToken``
            # per OAI-PMH §3.5.
            params = {"verb": "ListRecords", "resumptionToken": token}

        raise AppError(
            "backend_unavailable",
            f"OAI-PMH pagination exceeded {max_pages} pages; aborting.",
            {"scope": "oaipmh", "max_pages": max_pages},
            status_code=502,
        )
    finally:
        if close_after:
            client.close()


def ingest(
    *,
    url: str,
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    bulk_index: Any,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    client: httpx.Client | None = None,
) -> OAIImportResult:
    """Run a full harvest + bulk-index cycle.

    ``bulk_index`` is a callable (typically ``container.adapter.bulk_index``)
    that accepts a list of docs and returns ``(ingested, failed)``.
    Failures from any single batch are captured — the remaining
    pages keep streaming so a partial outage does not wipe a long
    harvest. A fatal error (unreachable endpoint, XML parse failure)
    aborts the run and is surfaced in ``OAIImportResult.error``.
    """
    ingested = 0
    failed = 0
    error: str | None = None
    chunk: list[dict[str, Any]] = []
    try:
        for doc in iter_records(
            url,
            metadata_prefix=metadata_prefix,
            set_spec=set_spec,
            client=client,
        ):
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
        logger.exception("oaipmh_ingest_failed", extra={"url": url})
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        logger.exception("oaipmh_ingest_unexpected_failure", extra={"url": url})
    return OAIImportResult(ingested=ingested, failed=failed, error=error)
