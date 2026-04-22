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

from dataclasses import dataclass, field
from typing import Any

from app.adapters.base import BackendAdapter
from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.adapters.opensearch.adapter import OpenSearchAdapter
from app.config.models import BackendAuthConfig
from app.errors import AppError
from app.logging import get_logger
from app.storage.sqlite_store import SQLiteStore

logger = get_logger("egg.admin_ui.setup")


# Valid wizard steps in order.  Kept as a tuple so index math ("what is
# the next step after ``source``?") stays obvious and typo-proof.
WIZARD_STEPS: tuple[str, ...] = (
    "backend",
    "source",
    "mapping",
    # S15 will append: "security", "exposure", "keys", "test", "done".
)


# Heuristic: EGG public field → ordered list of backend field-name hints.
# First match wins. Used by :func:`propose_mapping` to pre-populate the
# mapping screen so operators rarely have to edit manually.
_MAPPING_HINTS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier", "_id"),
    "type": ("type", "record_type", "doc_type"),
    "title": ("title", "name", "label", "dc_title"),
    "description": ("description", "abstract", "summary", "dc_description"),
    "creators": ("creator_csv", "creators", "creator", "author", "dc_creator"),
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

    # -- Persistence glue ---------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "backend": dict(self.backend),
            "source": dict(self.source),
            "detected_version": self.detected_version,
            "available_indices": list(self.available_indices),
            "available_fields": dict(self.available_fields),
            "mapping": {k: dict(v) for k, v in self.mapping.items()},
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SetupDraft:
        default = cls()
        backend = payload.get("backend") or default.backend
        source = payload.get("source") or default.source
        return cls(
            backend=dict(backend),
            source=dict(source),
            detected_version=payload.get("detected_version"),
            available_indices=list(payload.get("available_indices") or []),
            available_fields=dict(payload.get("available_fields") or {}),
            mapping={k: dict(v) for k, v in (payload.get("mapping") or {}).items()},
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


def propose_mapping(available_fields: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Heuristic default mapping from a flat field map.

    Returns the same shape the admin config expects (``{public_name:
    FieldMapping-dict}``). Only fills in the five canonical EGG fields;
    operators can add more on the mapping screen itself (Sprint 15).
    """
    proposal: dict[str, dict[str, Any]] = {}
    lower_map = {name.lower(): name for name in available_fields}
    for public_name, hints in _MAPPING_HINTS.items():
        chosen: str | None = None
        for hint in hints:
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
