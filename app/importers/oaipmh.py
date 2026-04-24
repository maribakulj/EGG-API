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

# Parser/type split: ``fromstring`` comes from ``defusedxml`` (blocks
# billion-laughs, quadratic-blowup and external-entity / XXE attacks),
# while the ``Element`` / ``ParseError`` types still come from stdlib
# because defusedxml re-exports only the parser entry points. OAI-PMH
# endpoints are admin-configured but can be untrusted or compromised
# third parties, so we treat their payloads as hostile by default.
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from defusedxml.ElementTree import fromstring as _safe_fromstring

from app.errors import AppError

logger = logging.getLogger("egg.importers.oaipmh")


RecordParser = Callable[
    [ET.Element, "ET.Element | None"],
    "dict[str, Any] | list[dict[str, Any]] | None",
]


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
    # defusedxml raises ``EntitiesForbidden``/``EntityBomb`` (subclass of
    # ``ValueError``) on XML bombs, and ``ET.ParseError`` on malformed
    # XML. We surface both as a single 502 so the caller doesn't need
    # to know which hostile shape the upstream was sending.
    try:
        return _safe_fromstring(xml_body)
    except (ET.ParseError, ValueError) as exc:
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
        # ``follow_redirects=False`` blocks SSRF via OAI-PMH redirects to
        # internal addresses (matches the Elasticsearch adapter's policy).
        # Operators whose endpoint relies on redirects must resolve the
        # final URL in their source config.
        client = httpx.Client(timeout=timeout, follow_redirects=False)
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
    record_parser: RecordParser | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one backend document per OAI-PMH record, following
    resumption tokens.

    ``max_pages`` is a safety ceiling: a misbehaving server returning
    an infinite loop of tokens will raise an ``AppError`` rather
    than ingesting forever. Each page stays in memory just long
    enough to parse and yield its records.

    ``record_parser`` takes one ``(header, metadata)`` tuple and
    returns the backend doc or ``None`` (deleted / malformed). The
    default parser is :func:`dc_record_to_doc`; Sprint 24 passes
    :func:`app.importers.lido.oai_record_to_doc` when the operator
    selects ``metadataPrefix=lido``.
    """

    parser: RecordParser = record_parser or dc_record_to_doc
    close_after = False
    if client is None:
        # ``follow_redirects=False`` blocks SSRF via OAI-PMH redirects to
        # internal addresses (matches the Elasticsearch adapter's policy).
        # Operators whose endpoint relies on redirects must resolve the
        # final URL in their source config.
        client = httpx.Client(timeout=timeout, follow_redirects=False)
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
                doc = parser(header, metadata)
                # EAD and other hierarchical records produce many docs
                # per OAI record, so the parser may return a list. All
                # other parsers return a single dict or ``None``.
                if doc is None:
                    continue
                if isinstance(doc, list):
                    for nested in doc:
                        if nested is not None:
                            yield nested
                else:
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
    record_parser: RecordParser | None = None,
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
            record_parser=record_parser,
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
