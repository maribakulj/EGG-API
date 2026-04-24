"""OAI-PMH 2.0 provider — EGG-API as an OAI source (Sprint 27).

Sprint 22 taught EGG to *consume* OAI-PMH. Sprint 27 closes the loop:
EGG now re-exposes its own indexed content as an OAI-PMH endpoint so
aggregators (Europeana, Gallica, Isidore, BASE, OpenAIRE) can harvest
the institution's collection through a protocol they already speak.
This turns every EGG install into a credible OAI node — a legal
requirement for some French regional programmes and a common ask
from CollEx and DARIAH partners.

The endpoint lives at ``GET /v1/oai`` (unauthenticated by spec). It
implements the six verbs from OAI-PMH 2.0 §3 with Dublin Core as the
only supported metadataPrefix:

* ``Identify`` — repository metadata (name, baseURL, admin email,
  earliest datestamp, deleted record policy).
* ``ListMetadataFormats`` — single entry for ``oai_dc``.
* ``ListSets`` — empty by design (EGG does not partition by set yet;
  returning ``noSetHierarchy`` keeps us compliant rather than lying).
* ``ListIdentifiers`` — headers only.
* ``ListRecords`` — headers + Dublin Core payload.
* ``GetRecord`` — one record by OAI identifier.

``ListRecords`` / ``ListIdentifiers`` support **resumption tokens**
(OAI-PMH §3.5). The token encodes the adapter cursor + the metadata
prefix; no server state is kept — harvesters can pause / resume across
process restarts.

All error surfaces follow OAI-PMH §3.6: the verb-level errors
(``badVerb``, ``badArgument``, ``cannotDisseminateFormat``,
``idDoesNotExist``, ``noRecordsMatch``, ``noSetHierarchy``,
``badResumptionToken``) are emitted inside the envelope with HTTP 200,
because OAI clients are trained to read the error code from the XML —
returning HTTP 4xx confuses the older tools.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from app.dependencies import container
from app.schemas.query import NormalizedQuery

logger = logging.getLogger("egg.oai_provider")


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
SUPPORTED_METADATA_PREFIXES: frozenset[str] = frozenset({"oai_dc"})

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _esc(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return xml_escape(str(value))


@dataclass
class _Token:
    cursor: str
    metadata_prefix: str

    def encode(self) -> str:
        raw = json.dumps({"c": self.cursor, "m": self.metadata_prefix}).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, raw: str) -> _Token | None:
        try:
            padding = "=" * (-len(raw) % 4)
            data = base64.urlsafe_b64decode(raw + padding)
            payload = json.loads(data.decode("utf-8"))
            return cls(cursor=str(payload["c"]), metadata_prefix=str(payload["m"]))
        except Exception:
            return None


def _normalize_identifier(identifier: str) -> str:
    """Strip the ``oai:<host>:`` prefix EGG emits on the wire."""

    if identifier.startswith("oai:") and ":" in identifier[4:]:
        return identifier.split(":", 2)[-1]
    return identifier


def _identifier_for(record_id: str, *, base_id: str) -> str:
    # OAI-PMH §2.4 identifier syntax: "oai:<repository-id>:<local>".
    return f"oai:{base_id}:{record_id}"


def _dublin_core_block(record: dict[str, Any]) -> str:
    """Build the ``<oai_dc:dc>`` payload for one record.

    Works off the public ``Record.model_dump()`` output (dict) so any
    deployment that already maps Dublin-Core-friendly fields (title,
    creators, description, date, subjects, languages, identifiers,
    rights) gets a correct export without extra configuration.
    """

    elements: list[str] = []

    def _add(tag: str, value: Any) -> None:
        if value is None or value == "" or value == []:
            return
        if isinstance(value, list):
            for item in value:
                text = _esc(item)
                if text:
                    elements.append(f"<dc:{tag}>{text}</dc:{tag}>")
        else:
            text = _esc(value)
            if text:
                elements.append(f"<dc:{tag}>{text}</dc:{tag}>")

    _add("title", record.get("title"))
    _add("creator", record.get("creators"))
    subjects = record.get("subjects")
    keywords = record.get("keywords")
    combined_subjects: list[str] = []
    if isinstance(subjects, list):
        combined_subjects.extend(s for s in subjects if s)
    elif subjects:
        combined_subjects.append(str(subjects))
    if isinstance(keywords, list):
        combined_subjects.extend(k for k in keywords if k)
    _add("subject", combined_subjects)
    _add("description", record.get("description"))
    date_info = record.get("date")
    if isinstance(date_info, dict):
        _add("date", date_info.get("display") or date_info.get("value"))
    else:
        _add("date", date_info)
    _add("language", record.get("languages"))
    _add("publisher", record.get("publisher"))
    _add("type", record.get("type"))
    identifiers = record.get("identifiers")
    if isinstance(identifiers, dict):
        for key in ("isbn", "issn", "doi", "url", "canonical"):
            _add("identifier", identifiers.get(key))
    rights = record.get("rights")
    if isinstance(rights, dict):
        _add("rights", rights.get("label") or rights.get("license"))
    else:
        _add("rights", rights)
    links = record.get("links")
    if isinstance(links, dict) and links.get("iiif_manifest"):
        _add("identifier", links["iiif_manifest"])

    return (
        f'<oai_dc:dc xmlns:oai_dc="{OAI_DC_NS}" xmlns:dc="{DC_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{OAI_DC_NS} '
        f'http://www.openarchives.org/OAI/2.0/oai_dc.xsd">' + "".join(elements) + "</oai_dc:dc>"
    )


def _envelope(*, request_url: str, verb: str, params: dict[str, str], body: str) -> str:
    """Wrap a verb response in the standard OAI-PMH envelope."""

    request_attrs = " ".join(
        f'{_esc(key)}="{_esc(value)}"' for key, value in sorted(params.items()) if value
    )
    attr_prefix = " " if request_attrs else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<OAI-PMH xmlns="{OAI_NS}" xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{OAI_NS} http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd">'
        f"<responseDate>{_now_iso()}</responseDate>"
        f'<request verb="{_esc(verb)}"{attr_prefix}{request_attrs}>{_esc(request_url)}</request>'
        f"{body}"
        "</OAI-PMH>"
    )


def _error(*, request_url: str, verb: str, params: dict[str, str], code: str, message: str) -> str:
    body = f'<error code="{_esc(code)}">{_esc(message)}</error>'
    return _envelope(request_url=request_url, verb=verb or "", params=params, body=body)


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------


def _repository_identity() -> tuple[str, str]:
    """Return ``(base_id, repository_name)`` from the active config.

    ``base_id`` keys the OAI identifier syntax (``oai:<base_id>:<local>``).
    The public hostname is not always available at import time, so we
    fall back to the config's ``public_base_url`` / ``repository_id``
    fields when set, otherwise to the literal ``"egg-api"``.
    """

    cfg = container.config_manager.config
    repo_name = getattr(cfg, "repository_name", None) or "EGG-API repository"
    base_id = (
        getattr(cfg, "repository_id", None)
        or (getattr(cfg, "public_base_url", "") or "")
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
        or "egg-api"
    )
    return base_id, repo_name


def _build_query(*, page_size: int, cursor: str | None) -> NormalizedQuery:
    return NormalizedQuery(
        q=None,
        page=1,
        page_size=page_size,
        facets=[],
        include_fields=[],
        filters={},
        cursor=cursor or None,
    )


def _search_page(
    page_size: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Return (records, next_cursor, total)."""

    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    nq = _build_query(page_size=page_size, cursor=cursor)
    payload = container.adapter.search(nq)
    hits = payload.get("hits", {}).get("hits", [])
    sources = [h.get("_source", {}) for h in hits]
    total = int(payload.get("hits", {}).get("total", {}).get("value", len(sources)))
    next_cursor: str | None = None
    if hits and len(hits) >= page_size:
        last_sort = hits[-1].get("sort")
        if last_sort:
            try:
                from app.adapters.elasticsearch.adapter import _encode_cursor

                next_cursor = _encode_cursor(last_sort)
            except Exception:
                next_cursor = None
    return sources, next_cursor, total


def _record_block(
    record_source: dict[str, Any],
    *,
    base_id: str,
    headers_only: bool,
    metadata_prefix: str,
) -> str:
    mapped = container.mapper.map_record(record_source)
    record_dict = mapped.model_dump(exclude_none=True)
    local_id = record_dict.get("id") or ""
    updated_at = ""
    ts = record_dict.get("timestamps") or {}
    if isinstance(ts, dict):
        updated_at = ts.get("updated_at") or ts.get("indexed_at") or ""
    datestamp = (updated_at or _now_iso()).split(".")[0]
    if not datestamp.endswith("Z"):
        datestamp = datestamp + "Z"
    header = (
        "<header>"
        f"<identifier>{_esc(_identifier_for(local_id, base_id=base_id))}</identifier>"
        f"<datestamp>{_esc(datestamp)}</datestamp>"
        "</header>"
    )
    if headers_only:
        return header
    metadata = _dublin_core_block(record_dict) if metadata_prefix == "oai_dc" else ""
    return f"<record>{header}<metadata>{metadata}</metadata></record>"


def _verb_identify(request_url: str, params: dict[str, str]) -> str:
    base_id, repo_name = _repository_identity()
    body = (
        "<Identify>"
        f"<repositoryName>{_esc(repo_name)}</repositoryName>"
        f"<baseURL>{_esc(request_url.split('?', 1)[0])}</baseURL>"
        "<protocolVersion>2.0</protocolVersion>"
        "<adminEmail>admin@localhost</adminEmail>"
        "<earliestDatestamp>1970-01-01T00:00:00Z</earliestDatestamp>"
        "<deletedRecord>no</deletedRecord>"
        "<granularity>YYYY-MM-DDThh:mm:ssZ</granularity>"
        f'<description><oai-identifier xmlns="http://www.openarchives.org/OAI/2.0/oai-identifier" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="http://www.openarchives.org/OAI/2.0/oai-identifier '
        f'http://www.openarchives.org/OAI/2.0/oai-identifier.xsd">'
        f"<scheme>oai</scheme>"
        f"<repositoryIdentifier>{_esc(base_id)}</repositoryIdentifier>"
        f"<delimiter>:</delimiter>"
        f"<sampleIdentifier>oai:{_esc(base_id)}:sample-1</sampleIdentifier>"
        f"</oai-identifier></description>"
        "</Identify>"
    )
    return _envelope(request_url=request_url, verb="Identify", params=params, body=body)


def _verb_list_metadata_formats(request_url: str, params: dict[str, str]) -> str:
    body = (
        "<ListMetadataFormats>"
        "<metadataFormat>"
        "<metadataPrefix>oai_dc</metadataPrefix>"
        "<schema>http://www.openarchives.org/OAI/2.0/oai_dc.xsd</schema>"
        "<metadataNamespace>http://www.openarchives.org/OAI/2.0/oai_dc/</metadataNamespace>"
        "</metadataFormat>"
        "</ListMetadataFormats>"
    )
    return _envelope(request_url=request_url, verb="ListMetadataFormats", params=params, body=body)


def _verb_list_sets(request_url: str, params: dict[str, str]) -> str:
    return _error(
        request_url=request_url,
        verb="ListSets",
        params=params,
        code="noSetHierarchy",
        message="This repository does not expose OAI-PMH sets.",
    )


def _verb_list(
    *,
    request_url: str,
    verb: str,
    params: dict[str, str],
    headers_only: bool,
) -> str:
    metadata_prefix = params.get("metadataPrefix") or ""
    resumption = params.get("resumptionToken") or ""
    cursor: str | None = None
    if resumption:
        token = _Token.decode(resumption)
        if token is None:
            return _error(
                request_url=request_url,
                verb=verb,
                params=params,
                code="badResumptionToken",
                message="The resumption token is not valid.",
            )
        metadata_prefix = token.metadata_prefix
        cursor = token.cursor
    if metadata_prefix not in SUPPORTED_METADATA_PREFIXES:
        return _error(
            request_url=request_url,
            verb=verb,
            params=params,
            code="cannotDisseminateFormat",
            message=f"metadataPrefix {metadata_prefix!r} is not supported.",
        )

    base_id, _ = _repository_identity()
    sources, next_cursor, total = _search_page(DEFAULT_PAGE_SIZE, cursor)
    if not sources and cursor is None:
        return _error(
            request_url=request_url,
            verb=verb,
            params=params,
            code="noRecordsMatch",
            message="The repository has no records to expose.",
        )

    record_blocks = [
        _record_block(
            src, base_id=base_id, headers_only=headers_only, metadata_prefix=metadata_prefix
        )
        for src in sources
    ]
    wrapper_open = "<ListRecords>" if verb == "ListRecords" else "<ListIdentifiers>"
    wrapper_close = "</ListRecords>" if verb == "ListRecords" else "</ListIdentifiers>"

    if headers_only:
        # ListIdentifiers emits bare <header> elements, not <record><header>…</record>.
        listing = "".join(record_blocks)
    else:
        listing = "".join(record_blocks)

    resumption_xml = ""
    if next_cursor:
        token_str = _Token(cursor=next_cursor, metadata_prefix=metadata_prefix).encode()
        resumption_xml = (
            f'<resumptionToken completeListSize="{total}">{_esc(token_str)}</resumptionToken>'
        )
    body = wrapper_open + listing + resumption_xml + wrapper_close
    return _envelope(request_url=request_url, verb=verb, params=params, body=body)


def _verb_get_record(request_url: str, params: dict[str, str]) -> str:
    identifier = params.get("identifier") or ""
    metadata_prefix = params.get("metadataPrefix") or ""
    if not identifier:
        return _error(
            request_url=request_url,
            verb="GetRecord",
            params=params,
            code="badArgument",
            message="identifier is required.",
        )
    if metadata_prefix not in SUPPORTED_METADATA_PREFIXES:
        return _error(
            request_url=request_url,
            verb="GetRecord",
            params=params,
            code="cannotDisseminateFormat",
            message=f"metadataPrefix {metadata_prefix!r} is not supported.",
        )
    record_id = _normalize_identifier(identifier)
    raw = container.adapter.get_record(record_id)
    if raw is None:
        return _error(
            request_url=request_url,
            verb="GetRecord",
            params=params,
            code="idDoesNotExist",
            message=f"No record with identifier {identifier!r}.",
        )
    base_id, _ = _repository_identity()
    body = (
        "<GetRecord>"
        + _record_block(raw, base_id=base_id, headers_only=False, metadata_prefix=metadata_prefix)
        + "</GetRecord>"
    )
    return _envelope(request_url=request_url, verb="GetRecord", params=params, body=body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_REQUIRED_PARAMS = {
    "Identify": set(),
    "ListMetadataFormats": set(),
    "ListSets": set(),
    "ListIdentifiers": {"metadataPrefix"},
    "ListRecords": {"metadataPrefix"},
    "GetRecord": {"identifier", "metadataPrefix"},
}


def handle(*, request_url: str, query_params: dict[str, str]) -> str:
    """Dispatch a raw OAI-PMH request to the matching verb handler.

    ``query_params`` is a flat ``dict`` (Starlette's ``QueryParams``
    coerced with ``dict(...)``). The function always returns well-formed
    XML; HTTP-level errors (malformed envelope, unexpected verb) are
    encoded inside the envelope per the OAI-PMH error-handling contract.
    """

    verb = query_params.get("verb") or ""
    params = {k: v for k, v in query_params.items() if k != "verb"}
    if verb not in _REQUIRED_PARAMS:
        return _error(
            request_url=request_url,
            verb=verb,
            params=params,
            code="badVerb",
            message=f"Unknown or missing verb: {verb!r}",
        )

    # resumptionToken is exclusive with other params per §3.5; skip the
    # required-arg check when it is present.
    if verb in {"ListRecords", "ListIdentifiers"} and query_params.get("resumptionToken"):
        pass
    else:
        missing = _REQUIRED_PARAMS[verb] - set(params)
        if missing:
            return _error(
                request_url=request_url,
                verb=verb,
                params=params,
                code="badArgument",
                message=f"Missing required argument(s): {', '.join(sorted(missing))}",
            )

    if verb == "Identify":
        return _verb_identify(request_url, params)
    if verb == "ListMetadataFormats":
        return _verb_list_metadata_formats(request_url, params)
    if verb == "ListSets":
        return _verb_list_sets(request_url, params)
    if verb == "ListIdentifiers":
        return _verb_list(request_url=request_url, verb=verb, params=params, headers_only=True)
    if verb == "ListRecords":
        return _verb_list(request_url=request_url, verb=verb, params=params, headers_only=False)
    if verb == "GetRecord":
        return _verb_get_record(request_url, params)
    # Defensive: _REQUIRED_PARAMS has a key for every verb above.
    return _error(  # pragma: no cover
        request_url=request_url,
        verb=verb,
        params=params,
        code="badVerb",
        message="Verb dispatch failed.",
    )


def build_request_url(*, scheme: str, host: str, path: str) -> str:
    """Helper the route uses to reconstruct the ``<request>`` URL."""

    return f"{scheme}://{host}{quote(path, safe='/:')}"
