"""Setup wizard helpers (Sprint 14).

The wizard lives outside the declarative config: operators step through
four screens (landing → backend → source → mapping) and the draft is
persisted per-admin in ``setup_drafts``. Only the final step
(:func:`promote_draft_to_config`, Sprint 15) writes to ``egg.yaml``.

Keeping the service thin on purpose:

- no FastAPI imports (so the logic is testable without a request);
- no ``container`` reference (so probes can hit a backend the active
  config does not know about);
- no template rendering (done by ``admin_ui/routes.py``).
"""

from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.adapters.base import BackendAdapter
from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.adapters.opensearch.adapter import OpenSearchAdapter
from app.config.models import AppConfig, BackendAuthConfig
from app.errors import AppError
from app.logging import get_logger
from app.schemas.query import NormalizedQuery
from app.storage.sqlite_store import SQLiteStore

logger = get_logger("egg.admin_ui.setup")


# Valid wizard steps in order.  Kept as a tuple so index math ("what is
# the next step after ``source``?") stays obvious and typo-proof.
WIZARD_STEPS: tuple[str, ...] = (
    "backend",
    "source",
    "mapping",
    "security",
    "exposure",
    "keys",
    "test",
    "done",
)


# Facet / sort / include-field catalog offered on the exposure screen.
# These are the defaults shipped in ``AppConfig``; the wizard surfaces
# them as checkboxes so non-technical operators pick from a menu rather
# than typing YAML.
_DEFAULT_EXPOSURE_FACETS: tuple[str, ...] = (
    "type",
    "language",
    "collection",
    "institution",
    "subject",
)
_DEFAULT_EXPOSURE_SORTS: tuple[str, ...] = (
    "relevance",
    "date_desc",
    "date_asc",
    "title_asc",
)
_DEFAULT_EXPOSURE_INCLUDE_FIELDS: tuple[str, ...] = (
    "id",
    "type",
    "title",
    "description",
    "creators",
)


def default_exposure() -> dict[str, list[str]]:
    """Return the pre-checked exposure options for a fresh draft."""
    return {
        "allowed_facets": list(_DEFAULT_EXPOSURE_FACETS),
        "allowed_sorts": list(_DEFAULT_EXPOSURE_SORTS),
        "allowed_include_fields": list(_DEFAULT_EXPOSURE_INCLUDE_FIELDS),
    }


EXPOSURE_CATALOG: dict[str, tuple[str, ...]] = {
    "allowed_facets": _DEFAULT_EXPOSURE_FACETS,
    "allowed_sorts": _DEFAULT_EXPOSURE_SORTS,
    "allowed_include_fields": _DEFAULT_EXPOSURE_INCLUDE_FIELDS,
}


# Heuristic: EGG public field → ordered list of backend field-name hints.
# First match wins. Used by :func:`propose_mapping` to pre-populate the
# mapping screen so operators rarely have to edit manually.
#
# Sprint 23: separate dictionaries per ``schema_profile`` so a museum
# deployment gets ``inventory_number`` / ``medium`` / ``dimensions``
# pre-filled from common backend column names, while a library keeps
# the lean default. ``custom`` returns nothing — the operator wants
# manual control.
_LIBRARY_MAPPING_HINTS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier", "_id"),
    "type": ("type", "record_type", "doc_type"),
    "title": ("title", "name", "label", "dc_title"),
    "description": ("description", "abstract", "summary", "dc_description"),
    "creators": ("creator_csv", "creators", "creator", "author", "dc_creator"),
}
_MUSEUM_MAPPING_HINTS: dict[str, tuple[str, ...]] = {
    "id": ("inventory_no", "inventory_number", "object_id", "id", "identifier", "_id"),
    "type": ("object_type", "objectworktype", "type", "doc_type"),
    "title": ("title", "object_name", "appellation", "label"),
    "description": ("description", "object_description", "summary"),
    "creators": ("artist", "maker", "creator", "creators", "author"),
    "museum.inventory_number": ("inventory_no", "inventory_number", "accession_no"),
    "museum.artist": ("artist", "maker"),
    "museum.medium": ("medium", "materials_techniques", "support"),
    "museum.dimensions": ("dimensions", "measurements", "size"),
    "museum.acquisition_date": ("acquisition_date", "acquired", "date_acquired"),
    "museum.current_location": ("current_location", "location", "venue"),
    "links.iiif_manifest": ("iiif_manifest", "iiif", "manifest_url", "manifest"),
    "links.thumbnail": ("thumbnail", "image", "preview"),
}
_ARCHIVE_MAPPING_HINTS: dict[str, tuple[str, ...]] = {
    "id": ("unitid", "ref_code", "id", "identifier", "_id"),
    "type": ("level", "type", "doc_type"),
    "title": ("unittitle", "title", "label"),
    "description": ("scopecontent", "description", "abstract"),
    "creators": ("origination", "creator", "creators", "author"),
}
# Backwards-compat alias for callers (and the S14 test) that import
# the original symbol name. Library is the lossless default.
_MAPPING_HINTS = _LIBRARY_MAPPING_HINTS

_HINTS_BY_PROFILE: dict[str, dict[str, tuple[str, ...]]] = {
    "library": _LIBRARY_MAPPING_HINTS,
    "museum": _MUSEUM_MAPPING_HINTS,
    "archive": _ARCHIVE_MAPPING_HINTS,
    "custom": {},
}


@dataclass
class SetupDraft:
    """In-memory representation of the wizard draft.

    Empty-field invariants: every key is always present with a sane
    default so templates can bind to ``draft.backend.url`` without
    ``{% if %}`` gymnastics. ``None`` values are explicit "operator
    hasn't answered yet" markers.
    """

    backend: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "elasticsearch",
            "url": "",
            "auth": {"mode": "none"},
        }
    )
    source: dict[str, Any] = field(default_factory=lambda: {"index": ""})
    detected_version: str | None = None
    available_indices: list[str] = field(default_factory=list)
    # Frozen snapshot of the backend mapping for the mapping screen. We
    # store only ``{field_name: es_type}`` — not the full ES payload —
    # so the draft row stays small even on wide indices.
    available_fields: dict[str, str] = field(default_factory=dict)
    mapping: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Sprint 23: schema-shape pivot. Drives the wizard's mapping
    # heuristic + which optional Record sub-blocks the publish step
    # exposes (museum / archive). Defaults to "library" so existing
    # drafts keep working.
    schema_profile: str = "library"
    # Sprint 15 additions.
    security_profile: str = "prudent"
    public_mode: str = "anonymous_allowed"
    exposure: dict[str, list[str]] = field(default_factory=default_exposure)
    # Tracks the first public key minted from the wizard. The raw
    # ``key`` is cleared after the operator navigates past the "keys"
    # screen — the draft never persists a reusable secret.
    first_key: dict[str, Any] | None = None
    # Most recent test-search result (query + hits summary) so the
    # "test" screen can display it after a page refresh.
    test_result: dict[str, Any] | None = None

    # -- Persistence glue ---------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "backend": dict(self.backend),
            "source": dict(self.source),
            "detected_version": self.detected_version,
            "available_indices": list(self.available_indices),
            "available_fields": dict(self.available_fields),
            "mapping": {k: dict(v) for k, v in self.mapping.items()},
            "schema_profile": self.schema_profile,
            "security_profile": self.security_profile,
            "public_mode": self.public_mode,
            "exposure": {k: list(v) for k, v in self.exposure.items()},
            "first_key": dict(self.first_key) if self.first_key else None,
            "test_result": dict(self.test_result) if self.test_result else None,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SetupDraft:
        default = cls()
        backend = payload.get("backend") or default.backend
        source = payload.get("source") or default.source
        exposure_raw = payload.get("exposure") or {}
        exposure = {k: list(exposure_raw.get(k) or v) for k, v in default_exposure().items()}
        return cls(
            backend=dict(backend),
            source=dict(source),
            detected_version=payload.get("detected_version"),
            available_indices=list(payload.get("available_indices") or []),
            available_fields=dict(payload.get("available_fields") or {}),
            mapping={k: dict(v) for k, v in (payload.get("mapping") or {}).items()},
            schema_profile=str(payload.get("schema_profile") or "library"),
            security_profile=str(payload.get("security_profile") or "prudent"),
            public_mode=str(payload.get("public_mode") or "anonymous_allowed"),
            exposure=exposure,
            first_key=dict(payload["first_key"]) if payload.get("first_key") else None,
            test_result=dict(payload["test_result"]) if payload.get("test_result") else None,
        )


class SetupDraftService:
    """Thin façade over the ``setup_drafts`` table."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def load(self, key_id: str) -> tuple[SetupDraft, str]:
        row = self._store.load_setup_draft(key_id)
        if row is None:
            return SetupDraft(), WIZARD_STEPS[0]
        payload, step = row
        if step not in WIZARD_STEPS:
            step = WIZARD_STEPS[0]
        return SetupDraft.from_json(payload), step

    def save(self, key_id: str, draft: SetupDraft, step: str) -> None:
        if step not in WIZARD_STEPS:
            raise ValueError(f"Unknown wizard step: {step!r}")
        self._store.save_setup_draft(key_id, draft.to_json(), step)

    def reset(self, key_id: str) -> None:
        self._store.delete_setup_draft(key_id)


# ---------------------------------------------------------------------------
# Probe helpers — build a one-shot adapter from the draft.
# ---------------------------------------------------------------------------


def build_probe_adapter(draft: SetupDraft) -> BackendAdapter:
    """Instantiate an adapter from the in-progress draft.

    Used by the wizard's "Test connection" and "Scan fields" actions so
    the operator can validate their inputs without first saving a full
    config and reloading the container. Never goes through
    ``container.adapter``: the running service keeps its production
    settings.
    """
    backend = draft.backend or {}
    btype = backend.get("type") or "elasticsearch"
    url = (backend.get("url") or "").strip()
    index = (draft.source or {}).get("index") or "records"
    if not url:
        raise AppError(
            "invalid_parameter",
            "Backend URL is required before testing the connection.",
            {"field": "backend.url"},
            status_code=400,
        )
    auth_payload = backend.get("auth") or {"mode": "none"}
    try:
        auth_cfg = BackendAuthConfig.model_validate(auth_payload)
    except Exception as exc:
        raise AppError(
            "invalid_parameter",
            f"Backend auth is misconfigured: {exc}",
            {"field": "backend.auth"},
            status_code=400,
        ) from exc
    kwargs: dict[str, Any] = {"auth_config": auth_cfg}
    if btype == "opensearch":
        return OpenSearchAdapter(url, str(index), **kwargs)
    return ElasticsearchAdapter(url, str(index), **kwargs)


def _flatten_es_properties(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Turn a nested ES ``properties`` block into ``{dotted_name: type}``.

    Nested objects are walked with dot-separated names; multi-fields
    (the ``fields`` sub-tree) are ignored because EGG only targets the
    primary analyzer chain.
    """
    out: dict[str, str] = {}
    for name, spec in (properties or {}).items():
        if not isinstance(spec, dict):
            continue
        full = f"{prefix}{name}"
        es_type = spec.get("type")
        if isinstance(es_type, str):
            out[full] = es_type
        nested = spec.get("properties")
        if isinstance(nested, dict):
            out.update(_flatten_es_properties(nested, prefix=f"{full}."))
    return out


def extract_index_choices(scan_payload: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Parse a ``scan_fields`` response into (index list, flat field map).

    When the operator has pinned an index via the draft, only that
    index's properties contribute to the field map. Otherwise we keep
    the union across every returned index so the source screen can
    show what exists before the operator commits.
    """
    indices: list[str] = sorted(scan_payload.keys())
    fields: dict[str, str] = {}
    for _, body in scan_payload.items():
        if not isinstance(body, dict):
            continue
        mappings = body.get("mappings")
        if not isinstance(mappings, dict):
            continue
        props = mappings.get("properties")
        if isinstance(props, dict):
            fields.update(_flatten_es_properties(props))
    return indices, fields


def draft_to_config(draft: SetupDraft, *, preserve: AppConfig | None = None) -> AppConfig:
    """Assemble a valid :class:`AppConfig` from a draft.

    ``preserve`` is the currently-active config, if any; its
    ``auth.bootstrap_admin_key``, ``storage.sqlite_path``, ``profiles``
    dictionary and ``cors`` block are kept so promoting a draft never
    regresses operator-only settings the wizard does not expose.
    """
    if preserve is None:
        preserve = AppConfig()
    mapping_in = draft.mapping or {}
    # Always keep at least ``id`` / ``type`` available to the mapper.
    # The mapping step enforces this, but guard here too so bad input
    # from a hand-crafted draft can't build a half-mapped service.
    exposure = draft.exposure or default_exposure()
    sorted_mapping = dict(mapping_in)
    if "id" not in sorted_mapping:
        sorted_mapping["id"] = {"source": "id", "mode": "direct", "criticality": "required"}
    if "type" not in sorted_mapping:
        sorted_mapping["type"] = {"source": "type", "mode": "direct", "criticality": "required"}

    # ``allowed_include_fields`` must be a subset of
    # {id, type} UNION mapping.keys(); filter whatever the draft carries
    # to keep the AppConfig cross-validator happy. Operators cannot
    # expose a public field they never mapped.
    structural = {"id", "type"}
    mapped = set(sorted_mapping.keys()) | structural
    include_source = exposure.get("allowed_include_fields") or preserve.allowed_include_fields
    filtered_includes = [f for f in include_source if f in mapped]
    for mandatory in ("id", "type"):
        if mandatory not in filtered_includes:
            filtered_includes.append(mandatory)

    payload: dict[str, Any] = {
        "backend": {
            "type": (draft.backend or {}).get("type") or preserve.backend.type,
            "url": (draft.backend or {}).get("url") or preserve.backend.url,
            "index": (draft.source or {}).get("index") or preserve.backend.index,
            "auth": (draft.backend or {}).get("auth") or {"mode": "none"},
        },
        "storage": {"sqlite_path": preserve.storage.sqlite_path},
        "schema_profile": (draft.schema_profile or preserve.schema_profile),
        "security_profile": draft.security_profile or preserve.security_profile,
        "profiles": {name: prof.model_dump() for name, prof in preserve.profiles.items()},
        "auth": {
            "public_mode": draft.public_mode or preserve.auth.public_mode,
            "admin_cookie_secure": preserve.auth.admin_cookie_secure,
            "admin_cookie_samesite": preserve.auth.admin_cookie_samesite,
            "admin_session_ttl_hours": preserve.auth.admin_session_ttl_hours,
        },
        "proxy": preserve.proxy.model_dump(),
        "cors": preserve.cors.model_dump(),
        "cache": preserve.cache.model_dump(),
        "rate_limit": preserve.rate_limit.model_dump(),
        "allowed_sorts": list(exposure.get("allowed_sorts") or preserve.allowed_sorts),
        "allowed_facets": list(exposure.get("allowed_facets") or preserve.allowed_facets),
        "allowed_include_fields": filtered_includes,
        "mapping": sorted_mapping,
    }
    return AppConfig.model_validate(payload)


def run_probe_search(adapter: BackendAdapter, query: str) -> dict[str, Any]:
    """Execute a minimal search against the probe adapter.

    Only used by step 7 ("Test") to give the operator a concrete
    success signal before publishing. Returns a shape the template can
    render directly (total hits + a couple of sample ids/titles).
    """
    nq = NormalizedQuery(
        q=query or None,
        page=1,
        page_size=3,
        sort=None,
        facets=[],
        include_fields=[],
        filters={},
        date_from=None,
        date_to=None,
        has_digital=None,
        has_iiif=None,
        cursor=None,
    )
    raw = adapter.search(nq)
    hits = (raw.get("hits") or {}).get("hits") or []
    total_raw = (raw.get("hits") or {}).get("total")
    if isinstance(total_raw, dict):
        total = int(total_raw.get("value") or 0)
    elif isinstance(total_raw, int):
        total = total_raw
    else:
        total = len(hits)
    samples = []
    for hit in hits[:3]:
        src = (hit.get("_source") or {}) if isinstance(hit, dict) else {}
        samples.append({"id": src.get("id"), "title": src.get("title")})
    return {"query": query, "total": total, "samples": samples}


def propose_mapping(
    available_fields: dict[str, str],
    *,
    profile: str = "library",
) -> dict[str, dict[str, Any]]:
    """Heuristic default mapping from a flat field map.

    Returns the same shape the admin config expects (``{public_name:
    FieldMapping-dict}``). The hint table is selected by ``profile``
    (Sprint 23), so a museum deployment automatically gets
    ``museum.inventory_number`` / ``museum.medium`` / ``links.iiif_manifest``
    pre-filled when the source carries the conventional column names.
    """
    hints = _HINTS_BY_PROFILE.get(profile, _LIBRARY_MAPPING_HINTS)
    proposal: dict[str, dict[str, Any]] = {}
    lower_map = {name.lower(): name for name in available_fields}
    for public_name, candidates in hints.items():
        chosen: str | None = None
        for hint in candidates:
            hit = lower_map.get(hint.lower())
            if hit is not None:
                chosen = hit
                break
        if chosen is None:
            # Fall back to identity if a field with the same name exists.
            chosen = lower_map.get(public_name.lower())
        if chosen is None:
            continue
        mode = "split_list" if public_name == "creators" and "csv" in chosen.lower() else "direct"
        criticality = "required" if public_name in {"id", "type"} else "optional"
        rule: dict[str, Any] = {
            "source": chosen,
            "mode": mode,
            "criticality": criticality,
        }
        if mode == "split_list":
            rule["separator"] = ";"
        proposal[public_name] = rule
    return proposal


# ---------------------------------------------------------------------------
# Backend auto-discovery.
#
# The wizard's first screen asks the operator to paste a backend URL.
# That's fine for IT-literate ops, but a librarian installing the
# desktop bundle typically has *one* Elasticsearch (or OpenSearch)
# running somewhere obvious — localhost, a docker-compose hostname —
# and has never had to type its URL. ``discover_backend_candidates``
# probes a short allowlist of well-known hosts in parallel and
# surfaces the ones that answer.
#
# Security posture:
#   - The allowlist is restricted to loopback + conventional
#     docker-compose hostnames; no arbitrary host/port scan. Operators
#     can extend it via EGG_DISCOVERY_HOSTS (comma-separated
#     "host[:port]" entries).
#   - Probes never send credentials. A protected backend therefore
#     answers with 401, which we report as "found but needs auth" so
#     the operator knows it's there without leaking anything.
#   - Overall deadline bounded by ``timeout_seconds`` * number of
#     candidates, capped to ~5 s total wall-clock via the thread
#     pool.
# ---------------------------------------------------------------------------


# Conventional hosts we probe by default. Adjust sparingly: adding a
# new entry here means *every* wizard user scans that host on every
# discovery click. Docker-compose nicknames ("elasticsearch", "opensearch")
# resolve only inside the compose network, so on a bare Mac they simply
# fail fast with a DNS error.
_DEFAULT_DISCOVERY_HOSTS: tuple[tuple[str, int], ...] = (
    ("localhost", 9200),
    ("127.0.0.1", 9200),
    ("localhost", 9201),
    ("elasticsearch", 9200),
    ("opensearch", 9200),
    ("opensearch-node1", 9200),
)


@dataclass(frozen=True)
class DiscoveryCandidate:
    """One probe result reported to the wizard UI."""

    url: str
    backend_type: str | None  # "elasticsearch", "opensearch", or None if unknown
    version: str | None
    status: str  # "ok", "needs_auth", "unsupported_version", "unreachable"
    message: str | None = None


def _env_discovery_hosts() -> list[tuple[str, int]]:
    raw = os.getenv("EGG_DISCOVERY_HOSTS", "").strip()
    if not raw:
        return []
    out: list[tuple[str, int]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, _, port = entry.partition(":")
        host = host.strip()
        if not host:
            continue
        try:
            parsed_port = int(port) if port else 9200
        except ValueError:
            continue
        out.append((host, parsed_port))
    return out


def _candidate_urls() -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for host, port in (*_DEFAULT_DISCOVERY_HOSTS, *_env_discovery_hosts()):
        url = f"http://{host}:{port}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _interpret_probe(url: str, resp: httpx.Response) -> DiscoveryCandidate:
    """Map an HTTP response from ``GET <backend>/`` to a candidate row."""
    status = resp.status_code
    if status in {401, 403}:
        return DiscoveryCandidate(
            url=url,
            backend_type=None,
            version=None,
            status="needs_auth",
            message=(
                "Backend responded but requires authentication. Use this URL and "
                "fill in the credentials on the next step."
            ),
        )
    if status != 200:
        return DiscoveryCandidate(
            url=url,
            backend_type=None,
            version=None,
            status="unreachable",
            message=f"Unexpected status {status}.",
        )
    try:
        body = resp.json() or {}
    except Exception:
        return DiscoveryCandidate(
            url=url,
            backend_type=None,
            version=None,
            status="unreachable",
            message="Response was not JSON — this is probably not a search backend.",
        )
    version_info = body.get("version", {}) if isinstance(body, dict) else {}
    version_number = str(version_info.get("number") or "") or None
    distribution = str(version_info.get("distribution") or "").strip().lower()
    tagline = str(body.get("tagline") or "").strip().lower()

    if distribution == "opensearch":
        btype: str | None = "opensearch"
    elif "elasticsearch" in tagline or "you know, for search" in tagline:
        btype = "elasticsearch"
    else:
        btype = None

    # Minimum-version gate mirrors the adapters' own check: ES ≥ 7, OS ≥ 1.
    if version_number:
        try:
            major = int(version_number.split(".", 1)[0])
        except ValueError:
            major = 0
        if btype == "elasticsearch" and major < 7:
            return DiscoveryCandidate(
                url=url,
                backend_type=btype,
                version=version_number,
                status="unsupported_version",
                message=f"Elasticsearch {version_number} is too old (need 7+).",
            )
        if btype == "opensearch" and major < 1:
            return DiscoveryCandidate(
                url=url,
                backend_type=btype,
                version=version_number,
                status="unsupported_version",
                message=f"OpenSearch {version_number} is too old (need 1+).",
            )

    if btype is None:
        return DiscoveryCandidate(
            url=url,
            backend_type=None,
            version=version_number,
            status="unreachable",
            message=("Something answered but did not identify as Elasticsearch/OpenSearch."),
        )
    return DiscoveryCandidate(url=url, backend_type=btype, version=version_number, status="ok")


def _probe_one(
    url: str, *, timeout_seconds: float, client: httpx.Client | None
) -> DiscoveryCandidate:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return DiscoveryCandidate(
            url=url,
            backend_type=None,
            version=None,
            status="unreachable",
            message="Unsupported URL scheme",
        )
    close_after = False
    if client is None:
        client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        close_after = True
    try:
        resp = client.get(url)
    except Exception as exc:
        logger.info("discovery_probe_failed_soft", url=url, error=str(exc))
        return DiscoveryCandidate(
            url=url,
            backend_type=None,
            version=None,
            status="unreachable",
            message=str(exc),
        )
    finally:
        if close_after:
            client.close()
    return _interpret_probe(url, resp)


def discover_backend_candidates(
    *,
    urls: list[str] | None = None,
    timeout_seconds: float = 1.5,
    max_workers: int = 6,
    client: httpx.Client | None = None,
) -> list[DiscoveryCandidate]:
    """Probe the allowlist in parallel and return every candidate.

    The return order is the probe order so templates can render
    "localhost first, docker-compose last". Reachable backends come
    with a ``status`` of ``ok`` / ``needs_auth`` / ``unsupported_version``;
    the wizard surfaces those with an "Use this" button. Unreachable
    entries are kept so the UI can explain why a well-known host did
    not answer.
    """
    probe_urls = urls if urls is not None else _candidate_urls()
    if not probe_urls:
        return []
    results: list[DiscoveryCandidate | None] = [None] * len(probe_urls)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_probe_one, url, timeout_seconds=timeout_seconds, client=client): idx
            for idx, url in enumerate(probe_urls)
        }
        for fut in concurrent.futures.as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
    return [r for r in results if r is not None]
