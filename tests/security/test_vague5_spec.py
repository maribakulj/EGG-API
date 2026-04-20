"""Regression tests for Vague 5 (M8-M9, L3): spec drift + gap coverage."""
from __future__ import annotations

import pytest

from app.config.models import AppConfig, FieldMapping
from app.dependencies import container
from app.rate_limit.limiter import (
    DEFAULT_MAX_REQUESTS_PER_WINDOW,
    DEFAULT_WINDOW_SECONDS,
    InMemoryRateLimiter,
)


# ---------------------------------------------------------------------------
# M8 — AppConfig cross-field validation
# ---------------------------------------------------------------------------

def test_m8_rejects_unknown_security_profile() -> None:
    with pytest.raises(ValueError, match="security_profile"):
        AppConfig.model_validate({"security_profile": "does-not-exist"})


def test_m8_rejects_invalid_auth_public_mode() -> None:
    with pytest.raises(ValueError, match="public_mode"):
        AppConfig.model_validate({"auth": {"public_mode": "bogus"}})


def test_m8_rejects_invalid_cors_mode() -> None:
    with pytest.raises(ValueError, match="cors.mode"):
        AppConfig.model_validate({"cors": {"mode": "on-maybe"}})


def test_m8_rejects_include_field_absent_from_mapping() -> None:
    with pytest.raises(ValueError, match="allowed_include_fields"):
        AppConfig.model_validate(
            {
                "allowed_include_fields": ["id", "type", "title", "nonexistent"],
            }
        )


def test_m8_required_mapping_rule_must_have_source() -> None:
    with pytest.raises(ValueError, match="required"):
        AppConfig.model_validate(
            {
                "mapping": {
                    "id": {"mode": "direct", "criticality": "required"},
                    "type": {"source": "type", "mode": "direct", "criticality": "required"},
                }
            }
        )


def test_m8_accepts_recommended_rule_with_template() -> None:
    cfg = AppConfig.model_validate(
        {
            "mapping": {
                "id": {"source": "id", "mode": "direct", "criticality": "required"},
                "type": {"source": "type", "mode": "direct", "criticality": "required"},
                "citation": {
                    "mode": "template",
                    "template": "$title ($year)",
                    "criticality": "recommended",
                },
            },
            "allowed_include_fields": ["id", "type", "citation"],
        }
    )
    assert "citation" in cfg.mapping


def test_m8_field_mapping_rejects_unknown_criticality() -> None:
    with pytest.raises(ValueError, match="criticality"):
        FieldMapping(source="x", criticality="bogus")


# ---------------------------------------------------------------------------
# M9 — Optional V1 endpoints
# ---------------------------------------------------------------------------

def test_m9_collections_returns_active_source(client) -> None:
    response = client.get("/v1/collections")
    assert response.status_code == 200
    body = response.json()
    assert "collections" in body
    assert body["collections"], "expected at least one collection"
    ids = {c["id"] for c in body["collections"]}
    assert "records" in ids


def test_m9_schema_exposes_active_mapping_and_allowlists(client) -> None:
    response = client.get("/v1/schema")
    assert response.status_code == 200
    body = response.json()
    # Keys we always expect in the schema payload.
    for key in ("fields", "allowed_include_fields", "allowed_facets", "allowed_sorts", "filters"):
        assert key in body
    field_names = {f["name"] for f in body["fields"]}
    assert "id" in field_names and "type" in field_names


def test_m9_suggest_is_501_with_typed_error_code(client) -> None:
    response = client.get("/v1/suggest?q=abc")
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


def test_m9_manifest_is_501_with_record_id_context(client) -> None:
    response = client.get("/v1/manifest/abc-123")
    assert response.status_code == 501
    body = response.json()
    assert body["error"]["code"] == "not_implemented"
    assert body["error"]["details"]["record_id"] == "abc-123"


# ---------------------------------------------------------------------------
# L3 — Gap tests (CORS rejection, empty results, session expiry via HTTP,
# suspended keys, rotation)
# ---------------------------------------------------------------------------

def test_l3_cors_preflight_rejected_when_disabled(client) -> None:
    # Default CorsConfig.mode == "off": no CORS headers should be emitted,
    # even on a cross-origin-style GET.
    response = client.get(
        "/v1/health",
        headers={"Origin": "https://evil.example"},
    )
    assert "access-control-allow-origin" not in {
        k.lower() for k in response.headers.keys()
    }


def test_l3_empty_hits_returns_empty_results(client) -> None:
    class EmptyAdapter:
        def health(self):
            return {"status": "green"}

        def search(self, _query):
            return {"hits": {"total": {"value": 0}, "hits": []}}

        @staticmethod
        def extract_facets(_payload):
            return {}

        def get_record(self, _record_id):
            return None

        def get_facets(self, _query):
            return {}

        def list_sources(self):
            return ["records"]

    original = container.adapter
    container.adapter = EmptyAdapter()
    try:
        response = client.get("/v1/search?q=no-match")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["results"] == []
        assert body["facets"] == {}
    finally:
        container.adapter = original


def test_l3_404_on_missing_record(client) -> None:
    response = client.get("/v1/records/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_l3_suspended_key_is_denied(client) -> None:
    # Create a key, suspend it, verify it's rejected on api_key_required mode.
    created = container.api_keys.create("suspend-test")
    container.api_keys.suspend(created.key_id)

    container.config_manager.config.auth.public_mode = "api_key_required"
    try:
        response = client.get(
            "/v1/search?q=x", headers={"x-api-key": created.key}
        )
        assert response.status_code == 401
    finally:
        container.config_manager.config.auth.public_mode = "anonymous_allowed"


def test_l3_revoked_key_does_not_resurrect_after_reactivate_on_wrong_id(client) -> None:
    created = container.api_keys.create("rotate-test")
    container.api_keys.revoke(created.key_id)
    # Reactivating by a typo key_id must not accidentally re-enable the key.
    container.api_keys.activate("rotate-test-typo")
    assert container.api_keys.validate(created.key) is False


def test_l3_session_expiry_forces_redirect_to_login(client, admin_headers) -> None:
    import sqlite3
    from datetime import datetime, timedelta, timezone

    # Login first.
    login = client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    token = None
    for raw in login.headers.get_list("set-cookie") if hasattr(login.headers, "get_list") else []:
        if raw.lower().startswith("egg_admin_session="):
            token = raw.split("=", 1)[1].split(";", 1)[0]
            break
    if token is None:
        # Fallback: read from the TestClient cookie jar.
        token = client.cookies.get("egg_admin_session")
    assert token, "expected a session cookie"

    # Force the session expired.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(container.store.db_path) as conn:
        conn.execute(
            "UPDATE ui_sessions SET expires_at = ? WHERE token = ?",
            (past, token),
        )
        conn.commit()

    # The dashboard should redirect back to login.
    response = client.get("/admin/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_l3_unsupported_operation_surfaces_on_deep_pagination(client) -> None:
    cfg = container.config_manager.config
    cfg.profiles[cfg.security_profile].max_depth = 10
    response = client.get("/v1/search?q=x&page=6&page_size=10")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_operation"


# ---------------------------------------------------------------------------
# L2 — Rate-limit defaults are named, not magic
# ---------------------------------------------------------------------------

def test_l2_rate_limiter_defaults_come_from_named_constants() -> None:
    limiter = InMemoryRateLimiter()
    assert limiter.max_requests == DEFAULT_MAX_REQUESTS_PER_WINDOW
    assert limiter.window_seconds == DEFAULT_WINDOW_SECONDS
