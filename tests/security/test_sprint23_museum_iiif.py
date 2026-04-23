"""Sprint 23 regression tests: museum schema + IIIF passthrough.

Covers:
- SchemaMapper honours dotted mapping keys (``museum.inventory_number``,
  ``links.iiif_manifest``) and groups them into nested Record blocks;
- empty museum blocks are NOT emitted (library deployments keep their
  slim response shape);
- AppConfig cross-validator accepts a dotted head name in
  ``allowed_include_fields`` when any dotted mapping key starts with it;
- ``/v1/manifest/{record_id}`` returns 302 → iiif_manifest URL when
  the record has one mapped, 404 otherwise;
- ``propose_mapping(profile="museum"/"archive")`` picks hints from
  the right dictionary;
- ``setup_service`` per-profile hints expose museum-specific slots;
- the wizard ``/admin/ui/setup/mapping/profile`` route persists the
  profile into the draft and rebuilds the mapping proposal.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.admin_ui.setup_service import (
    _HINTS_BY_PROFILE,
    SetupDraft,
    draft_to_config,
    propose_mapping,
)
from app.config.models import AppConfig
from app.dependencies import container
from app.mappers.schema_mapper import SchemaMapper

# ---------------------------------------------------------------------------
# Mapper: dotted keys → nested Record blocks
# ---------------------------------------------------------------------------


def _museum_config() -> AppConfig:
    """An AppConfig wiring museum-oriented dotted mapping keys."""
    return AppConfig.model_validate(
        {
            "schema_profile": "museum",
            "mapping": {
                "id": {"source": "inv_no", "mode": "direct", "criticality": "required"},
                "type": {"source": "type", "mode": "direct", "criticality": "required"},
                "title": {"source": "title", "mode": "direct"},
                "museum.inventory_number": {"source": "inv_no", "mode": "direct"},
                "museum.medium": {"source": "medium", "mode": "direct"},
                "museum.dimensions": {"source": "dims", "mode": "direct"},
                "links.iiif_manifest": {"source": "manifest_url", "mode": "direct"},
            },
            "allowed_include_fields": ["id", "type", "title", "museum", "links"],
        }
    )


def test_mapper_emits_museum_sub_block_when_mapped() -> None:
    mapper = SchemaMapper(_museum_config())
    doc = {
        "inv_no": "INV-42",
        "type": "painting",
        "title": "Self-portrait",
        "medium": "oil on canvas",
        "dims": "65 x 54 cm",
        "manifest_url": "https://iiif.example.org/iiif/42/manifest",
    }
    record = mapper.map_record(doc)
    assert record.museum is not None
    assert record.museum.inventory_number == "INV-42"
    assert record.museum.medium == "oil on canvas"
    assert record.museum.dimensions == "65 x 54 cm"
    assert record.links.iiif_manifest == "https://iiif.example.org/iiif/42/manifest"


def test_mapper_keeps_museum_none_when_all_museum_sources_empty() -> None:
    """Museum block collapses when *every* museum.* source is empty.

    The fixture wires ``museum.inventory_number`` to ``inv_no`` and
    the other museum fields to distinct sources (``medium``, ``dims``,
    ``manifest_url``). With none of those in the document, there is
    no museum.* value to carry — the block should drop.
    """
    mapper = SchemaMapper(_museum_config())
    # ``inv_no`` is empty but ``id`` carries a value, so the fallback
    # in map_record() keeps the record but every museum.* source
    # resolves to empty → the museum block drops.
    doc = {"id": "rec-1", "inv_no": "", "type": "record", "title": "No extras"}
    record = mapper.map_record(doc)
    assert record.museum is None


def test_library_config_does_not_emit_museum_block() -> None:
    """Default library config has no museum.* mapping → no museum key."""
    mapper = SchemaMapper(AppConfig())
    doc = {"id": "1", "type": "book", "title": "Library item"}
    record = mapper.map_record(doc)
    assert record.museum is None


def test_app_config_allows_museum_head_in_include_fields() -> None:
    # The cross-validator must accept "museum" as an include field
    # when at least one dotted mapping key starts with "museum.".
    cfg = AppConfig.model_validate(
        {
            "mapping": {
                "id": {"source": "id", "mode": "direct", "criticality": "required"},
                "type": {"source": "type", "mode": "direct", "criticality": "required"},
                "museum.inventory_number": {"source": "inv", "mode": "direct"},
            },
            "allowed_include_fields": ["id", "type", "museum"],
        }
    )
    assert "museum" in cfg.allowed_include_fields


# ---------------------------------------------------------------------------
# /v1/manifest/{record_id}
# ---------------------------------------------------------------------------


def test_manifest_route_redirects_when_iiif_is_mapped(client: TestClient) -> None:
    # Point the container adapter's get_record at a fixture that
    # includes an IIIF URL; also wire a mapping that exposes it.
    original_config = container.config_manager.config
    container.config_manager._config = AppConfig.model_validate(  # type: ignore[attr-defined]
        {
            "mapping": {
                "id": {"source": "id", "mode": "direct", "criticality": "required"},
                "type": {"source": "type", "mode": "direct", "criticality": "required"},
                "links.iiif_manifest": {"source": "manifest_url", "mode": "direct"},
            },
            "allowed_include_fields": ["id", "type", "links"],
        }
    )
    container._state = container._state.__class__(  # type: ignore[attr-defined]
        **{**container._state.__dict__, "mapper": SchemaMapper(container.config_manager.config)},  # type: ignore[attr-defined]
    )

    original_get = container.adapter.get_record  # type: ignore[attr-defined]

    def _fake_get(record_id: str):
        if record_id == "with-iiif":
            return {
                "id": "with-iiif",
                "type": "painting",
                "manifest_url": "https://iiif.example.org/iiif/42/manifest",
            }
        if record_id == "no-iiif":
            return {"id": "no-iiif", "type": "painting"}
        return None

    container.adapter.get_record = _fake_get  # type: ignore[attr-defined]
    try:
        resp = client.get("/v1/manifest/with-iiif", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://iiif.example.org/iiif/42/manifest"

        # Record exists but no IIIF link → 404.
        resp = client.get("/v1/manifest/no-iiif", follow_redirects=False)
        assert resp.status_code == 404

        # Missing record → 404.
        resp = client.get("/v1/manifest/does-not-exist", follow_redirects=False)
        assert resp.status_code == 404
    finally:
        container.adapter.get_record = original_get  # type: ignore[attr-defined]
        container.config_manager._config = original_config  # type: ignore[attr-defined]
        container._state = container._state.__class__(  # type: ignore[attr-defined]
            **{
                **container._state.__dict__,  # type: ignore[attr-defined]
                "mapper": SchemaMapper(original_config),
            },
        )


# ---------------------------------------------------------------------------
# propose_mapping honours the profile
# ---------------------------------------------------------------------------


def test_propose_mapping_museum_picks_inventory_and_iiif() -> None:
    fields = {
        "inventory_no": "keyword",
        "type": "keyword",
        "title": "text",
        "medium": "keyword",
        "dimensions": "keyword",
        "iiif_manifest": "keyword",
    }
    proposal = propose_mapping(fields, profile="museum")
    assert proposal["id"]["source"] == "inventory_no"
    assert proposal["museum.inventory_number"]["source"] == "inventory_no"
    assert proposal["museum.medium"]["source"] == "medium"
    assert proposal["museum.dimensions"]["source"] == "dimensions"
    assert proposal["links.iiif_manifest"]["source"] == "iiif_manifest"


def test_propose_mapping_archive_picks_archival_fields() -> None:
    fields = {
        "unitid": "keyword",
        "unittitle": "text",
        "scopecontent": "text",
        "origination": "keyword",
        "level": "keyword",
    }
    proposal = propose_mapping(fields, profile="archive")
    assert proposal["id"]["source"] == "unitid"
    assert proposal["type"]["source"] == "level"
    assert proposal["title"]["source"] == "unittitle"
    assert proposal["description"]["source"] == "scopecontent"


def test_propose_mapping_custom_profile_returns_nothing() -> None:
    fields = {"id": "keyword", "title": "text"}
    assert propose_mapping(fields, profile="custom") == {}


def test_hint_registry_has_all_profiles() -> None:
    assert set(_HINTS_BY_PROFILE) == {"library", "museum", "archive", "custom"}


# ---------------------------------------------------------------------------
# Draft → AppConfig
# ---------------------------------------------------------------------------


def test_draft_to_config_propagates_schema_profile() -> None:
    draft = SetupDraft()
    draft.backend["url"] = "http://es.test:9200"
    draft.source["index"] = "records"
    draft.schema_profile = "museum"
    draft.mapping = {
        "id": {"source": "id", "mode": "direct", "criticality": "required"},
        "type": {"source": "type", "mode": "direct", "criticality": "required"},
    }
    cfg = draft_to_config(draft)
    assert cfg.schema_profile == "museum"


def test_setup_draft_round_trip_preserves_schema_profile() -> None:
    draft = SetupDraft()
    draft.schema_profile = "archive"
    serialized = draft.to_json()
    reloaded = SetupDraft.from_json(serialized)
    assert reloaded.schema_profile == "archive"


# ---------------------------------------------------------------------------
# /admin/ui/setup/mapping/profile
# ---------------------------------------------------------------------------


def test_wizard_profile_route_rebuilds_mapping(client: TestClient, admin_ui_session: str) -> None:
    # Seed a draft that already reached the mapping step with some
    # museum-shaped fields available.
    container.store.save_setup_draft(
        "admin",
        {
            "backend": {"type": "elasticsearch", "url": "http://x", "auth": {"mode": "none"}},
            "source": {"index": "records"},
            "available_fields": {
                "inventory_no": "keyword",
                "title": "text",
                "medium": "keyword",
                "iiif_manifest": "keyword",
            },
            "available_indices": ["records"],
            "mapping": {
                "id": {"source": "inventory_no", "mode": "direct", "criticality": "required"},
                "type": {"source": "type", "mode": "direct", "criticality": "required"},
            },
            "schema_profile": "library",
            "detected_version": None,
        },
        "mapping",
    )

    resp = client.post(
        "/admin/ui/setup/mapping/profile",
        data={"csrf_token": admin_ui_session, "schema_profile": "museum"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup/mapping"

    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    assert payload["schema_profile"] == "museum"
    # museum-specific slots were proposed.
    assert "museum.inventory_number" in payload["mapping"]
    assert "links.iiif_manifest" in payload["mapping"]


def test_wizard_profile_route_custom_clears_mapping(
    client: TestClient, admin_ui_session: str
) -> None:
    container.store.save_setup_draft(
        "admin",
        {
            "backend": {"type": "elasticsearch", "url": "http://x", "auth": {"mode": "none"}},
            "source": {"index": "records"},
            "available_fields": {"id": "keyword", "title": "text"},
            "mapping": {
                "id": {"source": "id", "mode": "direct", "criticality": "required"},
                "title": {"source": "title", "mode": "direct"},
            },
            "schema_profile": "library",
        },
        "mapping",
    )
    client.post(
        "/admin/ui/setup/mapping/profile",
        data={"csrf_token": admin_ui_session, "schema_profile": "custom"},
        follow_redirects=False,
    )
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    assert payload["schema_profile"] == "custom"
    assert payload["mapping"] == {}


def test_wizard_profile_route_rejects_unknown_profile(
    client: TestClient, admin_ui_session: str
) -> None:
    container.store.save_setup_draft(
        "admin",
        {"backend": {"url": "http://x"}, "source": {"index": "i"}, "schema_profile": "library"},
        "mapping",
    )
    client.post(
        "/admin/ui/setup/mapping/profile",
        data={"csrf_token": admin_ui_session, "schema_profile": "hacker"},
        follow_redirects=False,
    )
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    # Bad input silently coerces back to the default; never persists
    # a junk profile into AppConfig.
    assert payload["schema_profile"] == "library"


def test_wizard_profile_submit_requires_login(client: TestClient) -> None:
    resp = client.post("/admin/ui/setup/mapping/profile", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
