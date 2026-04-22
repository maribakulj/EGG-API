"""Sprint 14 regression tests: admin UI setup wizard (screens 1-4).

Covers:

- landing page: detects whether a draft exists and proposes resume vs.
  start-over;
- start/reset flow writes/clears the ``setup_drafts`` row for the
  signed-in admin ``key_id``;
- backend step persists URL + auth mode and can probe the FakeAdapter
  via build_probe_adapter;
- source step's scan_fields button pulls index names + available
  fields out of the adapter payload;
- mapping step pre-fills a proposal from the heuristic, rejects a
  submission missing the required ``id``/``type`` bindings, and keeps
  the draft in place on error.

Nothing here writes to the on-disk YAML: the wizard stops at the end
of step 3 until Sprint 15 adds the publish flow.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.admin_ui.setup_service import (
    SetupDraft,
    extract_index_choices,
    propose_mapping,
)
from app.dependencies import container

# ---------------------------------------------------------------------------
# Landing & lifecycle
# ---------------------------------------------------------------------------


def test_landing_unauthenticated_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/admin/ui/setup", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_landing_shows_start_button_when_no_draft(
    client: TestClient, admin_ui_session: str
) -> None:
    resp = client.get("/admin/ui/setup")
    assert resp.status_code == 200
    assert "Start the wizard" in resp.text


def test_start_creates_draft_and_redirects_to_backend(
    client: TestClient, admin_ui_session: str
) -> None:
    resp = client.post(
        "/admin/ui/setup/start",
        data={"csrf_token": admin_ui_session},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup/backend"
    assert container.store.load_setup_draft("admin") is not None


def test_reset_clears_draft(client: TestClient, admin_ui_session: str) -> None:
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    resp = client.post(
        "/admin/ui/setup/reset",
        data={"csrf_token": admin_ui_session},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup"
    assert container.store.load_setup_draft("admin") is None


# ---------------------------------------------------------------------------
# Step 1 — backend
# ---------------------------------------------------------------------------


def test_backend_submit_persists_url_and_auth(client: TestClient, admin_ui_session: str) -> None:
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    resp = client.post(
        "/admin/ui/setup/backend",
        data={
            "csrf_token": admin_ui_session,
            "backend_type": "elasticsearch",
            "backend_url": "http://es.example.org:9200",
            "auth_mode": "bearer",
            "auth_token_env": "MY_TOKEN_ENV",
            "action": "next",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup/source"
    row = container.store.load_setup_draft("admin")
    assert row is not None
    payload, step = row
    assert step == "source"
    assert payload["backend"]["url"] == "http://es.example.org:9200"
    assert payload["backend"]["auth"]["mode"] == "bearer"
    assert payload["backend"]["auth"]["token_env"] == "MY_TOKEN_ENV"


def test_backend_submit_rejects_empty_url(client: TestClient, admin_ui_session: str) -> None:
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    resp = client.post(
        "/admin/ui/setup/backend",
        data={
            "csrf_token": admin_ui_session,
            "backend_type": "elasticsearch",
            "backend_url": "",
            "auth_mode": "none",
            "action": "next",
        },
    )
    assert resp.status_code == 400
    assert "Backend URL is required" in resp.text


def test_backend_test_action_probes_fake_adapter_and_records_version(
    client: TestClient, admin_ui_session: str
) -> None:
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    # The wizard builds its own adapter from the draft.  We can't point
    # that adapter at the FakeAdapter directly, so we stub it here via a
    # monkey-patch on build_probe_adapter's resolver.  Simpler: swap in
    # the in-test FakeAdapter after it's built.
    # ``admin_ui.routes`` imported build_probe_adapter by name (early
    # binding), so we patch the attribute in that module.
    import app.admin_ui.routes as routes_mod

    original = routes_mod.build_probe_adapter
    routes_mod.build_probe_adapter = lambda _d: container.adapter  # type: ignore[assignment]
    try:
        resp = client.post(
            "/admin/ui/setup/backend",
            data={
                "csrf_token": admin_ui_session,
                "backend_type": "elasticsearch",
                "backend_url": "http://es.example.org:9200",
                "auth_mode": "none",
                "action": "test",
            },
        )
    finally:
        routes_mod.build_probe_adapter = original
    assert resp.status_code == 200
    assert "Connection successful" in resp.text
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    assert payload["detected_version"] == "8.0.0"  # FakeAdapter's fixture


# ---------------------------------------------------------------------------
# Step 2 — source
# ---------------------------------------------------------------------------


def test_source_page_redirects_when_backend_missing(
    client: TestClient, admin_ui_session: str
) -> None:
    # Start the draft but skip the backend step → source should bounce back.
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    resp = client.get("/admin/ui/setup/source", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup/backend"


def test_source_scan_pulls_indices_and_fields(client: TestClient, admin_ui_session: str) -> None:
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    client.post(
        "/admin/ui/setup/backend",
        data={
            "csrf_token": admin_ui_session,
            "backend_type": "elasticsearch",
            "backend_url": "http://es.example.org:9200",
            "auth_mode": "none",
            "action": "next",
        },
    )
    import app.admin_ui.routes as routes_mod

    original = routes_mod.build_probe_adapter
    routes_mod.build_probe_adapter = lambda _d: container.adapter  # type: ignore[assignment]
    try:
        resp = client.post(
            "/admin/ui/setup/source",
            data={"csrf_token": admin_ui_session, "index": "", "action": "scan"},
        )
    finally:
        routes_mod.build_probe_adapter = original
    assert resp.status_code == 200
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    assert payload["available_indices"] == ["records"]
    # FakeAdapter.scan_fields exposes one property ``title``.
    assert "title" in payload["available_fields"]
    # Single index: wizard auto-picks it so the operator can go straight
    # to the next step.
    assert payload["source"]["index"] == "records"


def test_source_next_without_index_shows_error(client: TestClient, admin_ui_session: str) -> None:
    client.post("/admin/ui/setup/start", data={"csrf_token": admin_ui_session})
    client.post(
        "/admin/ui/setup/backend",
        data={
            "csrf_token": admin_ui_session,
            "backend_type": "elasticsearch",
            "backend_url": "http://es.example.org:9200",
            "auth_mode": "none",
            "action": "next",
        },
    )
    resp = client.post(
        "/admin/ui/setup/source",
        data={"csrf_token": admin_ui_session, "index": "", "action": "next"},
    )
    assert resp.status_code == 400
    assert "index" in resp.text.lower()


# ---------------------------------------------------------------------------
# Step 3 — mapping
# ---------------------------------------------------------------------------


def test_mapping_page_auto_proposes_when_fields_known(
    client: TestClient, admin_ui_session: str
) -> None:
    # Seed a draft that has fields available but no mapping yet.
    container.store.save_setup_draft(
        "admin",
        {
            "backend": {"type": "elasticsearch", "url": "http://x", "auth": {"mode": "none"}},
            "source": {"index": "records"},
            "detected_version": None,
            "available_indices": ["records"],
            "available_fields": {
                "id": "keyword",
                "type": "keyword",
                "title": "text",
                "creator_csv": "keyword",
            },
            "mapping": {},
        },
        "mapping",
    )
    resp = client.get("/admin/ui/setup/mapping")
    assert resp.status_code == 200
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    # propose_mapping should have filled id/type/title/creators at least.
    assert payload["mapping"].get("id", {}).get("source") == "id"
    assert payload["mapping"].get("title", {}).get("source") == "title"
    assert payload["mapping"].get("creators", {}).get("mode") == "split_list"


def test_mapping_rejects_missing_id_or_type(client: TestClient, admin_ui_session: str) -> None:
    container.store.save_setup_draft(
        "admin",
        {
            "backend": {"type": "elasticsearch", "url": "http://x", "auth": {"mode": "none"}},
            "source": {"index": "records"},
            "available_fields": {"title": "text"},
            "available_indices": ["records"],
            "mapping": {},
            "detected_version": None,
        },
        "mapping",
    )
    resp = client.post(
        "/admin/ui/setup/mapping",
        data={
            "csrf_token": admin_ui_session,
            "source__id": "",
            "mode__id": "direct",
            "source__type": "",
            "mode__type": "direct",
            "source__title": "title",
            "mode__title": "direct",
            "source__description": "",
            "mode__description": "direct",
            "source__creators": "",
            "mode__creators": "direct",
            "action": "next",
        },
    )
    assert resp.status_code == 400
    assert "'id'" in resp.text or "id" in resp.text
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    # Title is preserved even on error so the operator does not lose work.
    assert payload["mapping"]["title"]["source"] == "title"


def test_mapping_accepts_valid_submission(client: TestClient, admin_ui_session: str) -> None:
    container.store.save_setup_draft(
        "admin",
        {
            "backend": {"type": "elasticsearch", "url": "http://x", "auth": {"mode": "none"}},
            "source": {"index": "records"},
            "available_fields": {"id": "keyword", "type": "keyword", "title": "text"},
            "available_indices": ["records"],
            "mapping": {},
            "detected_version": None,
        },
        "mapping",
    )
    resp = client.post(
        "/admin/ui/setup/mapping",
        data={
            "csrf_token": admin_ui_session,
            "source__id": "id",
            "mode__id": "direct",
            "source__type": "type",
            "mode__type": "direct",
            "source__title": "title",
            "mode__title": "direct",
            "source__description": "",
            "mode__description": "direct",
            "source__creators": "",
            "mode__creators": "direct",
            "action": "next",
        },
    )
    assert resp.status_code == 200
    assert "Mapping saved" in resp.text
    payload, _ = container.store.load_setup_draft("admin")  # type: ignore[misc]
    assert payload["mapping"]["id"]["source"] == "id"
    assert payload["mapping"]["type"]["source"] == "type"
    assert payload["mapping"]["title"]["source"] == "title"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_extract_index_choices_flattens_nested_properties() -> None:
    payload = {
        "records": {
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "metadata": {
                        "properties": {
                            "created_at": {"type": "date"},
                        }
                    },
                }
            }
        },
    }
    indices, fields = extract_index_choices(payload)
    assert indices == ["records"]
    assert fields["id"] == "keyword"
    assert fields["metadata.created_at"] == "date"


def test_propose_mapping_matches_hints_case_insensitively() -> None:
    fields = {"IDENTIFIER": "keyword", "Title": "text", "author": "keyword"}
    proposal = propose_mapping(fields)
    assert proposal["id"]["source"] == "IDENTIFIER"
    assert proposal["title"]["source"] == "Title"
    assert proposal["creators"]["source"] == "author"


def test_setup_draft_round_trip() -> None:
    draft = SetupDraft()
    draft.backend["url"] = "http://test"
    serialized = draft.to_json()
    assert SetupDraft.from_json(serialized).backend["url"] == "http://test"
