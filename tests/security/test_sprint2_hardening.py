"""Regression tests for Sprint 2 UI hardening (S2.1 - S2.8)."""

from __future__ import annotations

import pytest

from app.admin_ui.auth import _csrf_for_session
from app.config.models import AppConfig
from app.dependencies import container

# ---------------------------------------------------------------------------
# S2.8 — cookie samesite/secure cross-validation
# ---------------------------------------------------------------------------


def test_s2_8_samesite_none_requires_secure_cookie() -> None:
    with pytest.raises(ValueError, match="admin_cookie_secure"):
        AppConfig.model_validate(
            {"auth": {"admin_cookie_samesite": "none", "admin_cookie_secure": False}}
        )


def test_s2_8_samesite_none_with_secure_is_accepted() -> None:
    cfg = AppConfig.model_validate(
        {"auth": {"admin_cookie_samesite": "none", "admin_cookie_secure": True}}
    )
    assert cfg.auth.admin_cookie_samesite == "none"


def test_s2_8_rejects_unknown_samesite_value() -> None:
    with pytest.raises(ValueError, match="admin_cookie_samesite"):
        AppConfig.model_validate({"auth": {"admin_cookie_samesite": "bogus"}})


# ---------------------------------------------------------------------------
# S2.1 — CSRF protection on every admin UI POST
# ---------------------------------------------------------------------------


def test_s2_1_config_update_without_csrf_rejected(client, admin_ui_session) -> None:
    # Fixture logs in (+mints a valid CSRF token) but we intentionally omit
    # the token here: the middleware must refuse the POST.
    response = client.post("/admin/ui/config", data={"backend_url": "http://x:9200"})
    assert response.status_code == 403
    assert "CSRF check failed" in response.text


def test_s2_1_keys_create_without_csrf_rejected(client, admin_ui_session) -> None:
    response = client.post("/admin/ui/keys/create", data={"key_id": "attempt"})
    assert response.status_code == 403


def test_s2_1_keys_status_without_csrf_rejected(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/admin/status", data={"action": "revoke"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_s2_1_logout_without_csrf_rejected(client, admin_ui_session) -> None:
    response = client.post("/admin/logout")
    assert response.status_code == 403


def test_s2_1_valid_csrf_accepted(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/create",
        data={"key_id": "good-key", "csrf_token": admin_ui_session},
    )
    assert response.status_code == 200
    assert "Copy it now" in response.text


def test_s2_1_wrong_csrf_rejected(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/create",
        data={"key_id": "evil", "csrf_token": "tampered"},
    )
    assert response.status_code == 403


def test_s2_1_csrf_token_rendered_in_forms(client, admin_ui_session) -> None:
    page = client.get("/admin/ui/config")
    assert page.status_code == 200
    assert 'name="csrf_token"' in page.text
    # Token must match what the session derives.
    session_token = client.cookies.get("egg_admin_session")
    expected = _csrf_for_session(session_token)
    assert expected in page.text


def test_s2_1_header_csrf_fallback_accepted(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/create",
        data={"key_id": "via-header"},
        headers={"x-csrf-token": admin_ui_session},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# S2.3 — UI error messages never leak exception internals
# ---------------------------------------------------------------------------


def test_s2_3_config_error_message_is_generic(client, admin_ui_session) -> None:
    cfg = container.config_manager.config
    response = client.post(
        "/admin/ui/config",
        data={
            "csrf_token": admin_ui_session,
            "backend_url": "http://ex:9200",
            "backend_index": "x",
            "security_profile": "not-a-profile",  # triggers validation error
            "public_mode": cfg.auth.public_mode,
            "sqlite_path": cfg.storage.sqlite_path,
            "allow_empty_query": "false",
            "page_size_default": "20",
            "page_size_max": "50",
            "max_depth": "2000",
        },
    )
    assert response.status_code == 400
    body = response.text
    # Generic user-facing copy only.
    assert "Unable to save configuration" in body
    # Pydantic internals must not leak.
    assert "ValidationError" not in body
    assert "pydantic" not in body.lower()


# ---------------------------------------------------------------------------
# S2.5 — logout-everywhere invalidates every session for the key_id
# ---------------------------------------------------------------------------


def test_s2_5_logout_everywhere_kills_sibling_sessions(
    client, admin_headers, admin_ui_session
) -> None:
    # The fixture holds one session. Open a second one from a fresh client
    # against the same admin key (same key_id).
    from fastapi.testclient import TestClient

    from app.main import app

    client2 = TestClient(app)
    login = client2.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert client2.get("/admin/ui").status_code == 200

    # Client 1 signs out of everywhere.
    response = client.post(
        "/admin/logout-everywhere",
        data={"csrf_token": admin_ui_session},
        follow_redirects=False,
    )
    assert response.status_code == 303

    # Client 2's session is now gone.
    page = client2.get("/admin/ui", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# S2.6 — admin key rotation
# ---------------------------------------------------------------------------


def test_s2_6_rotate_returns_new_secret_and_invalidates_sessions(
    client, admin_headers, admin_ui_session
) -> None:
    # Create a second key to rotate so we don't interfere with the admin key.
    created = container.api_keys.create("rotate-target")
    old_secret = created.key

    response = client.post(
        "/admin/ui/keys/rotate-target/rotate",
        data={"csrf_token": admin_ui_session},
    )
    assert response.status_code == 200
    # The old secret must no longer validate; we can't inspect the new one
    # from here, but the message / new_key panel must be rendered.
    assert "rotated" in response.text.lower()
    assert container.api_keys.validate(old_secret) is False


def test_s2_6_rotate_unknown_key_returns_404(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/does-not-exist/rotate",
        data={"csrf_token": admin_ui_session},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# S2.7 — revoke_by_key_id and revoke_by_secret are distinct APIs
# ---------------------------------------------------------------------------


def test_s2_7_revoke_by_key_id_flips_status() -> None:
    created = container.api_keys.create("split-rev-id")
    assert container.api_keys.revoke_by_key_id("split-rev-id") is True
    assert container.api_keys.validate(created.key) is False


def test_s2_7_revoke_by_secret_flips_status() -> None:
    created = container.api_keys.create("split-rev-sec")
    assert container.api_keys.revoke_by_secret(created.key) is True
    assert container.api_keys.validate(created.key) is False


def test_s2_7_revoke_by_key_id_returns_false_for_unknown() -> None:
    assert container.api_keys.revoke_by_key_id("nope-does-not-exist") is False


def test_s2_7_legacy_revoke_falls_back() -> None:
    created = container.api_keys.create("legacy-revoke")
    # Legacy single-arg API still works for both shapes.
    assert container.api_keys.revoke("legacy-revoke") is True
    assert container.api_keys.validate(created.key) is False


# ---------------------------------------------------------------------------
# S2.4 — proxy headers middleware
# ---------------------------------------------------------------------------


def test_s2_4_proxy_config_empty_by_default() -> None:
    cfg = AppConfig()
    assert cfg.proxy.trusted_proxies == []


def test_s2_4_proxy_config_accepts_list() -> None:
    cfg = AppConfig.model_validate({"proxy": {"trusted_proxies": ["10.0.0.1", "10.0.0.2"]}})
    assert cfg.proxy.trusted_proxies == ["10.0.0.1", "10.0.0.2"]


# ---------------------------------------------------------------------------
# Misc UI coverage: dashboard error-path, logout flows, CSRF header parsing
# ---------------------------------------------------------------------------


def test_dashboard_shows_backend_unavailable_when_adapter_fails(client, admin_ui_session) -> None:
    class _Broken:
        def health(self):
            raise RuntimeError("down")

        def list_sources(self):
            return []

    original = container.adapter
    container.adapter = _Broken()
    try:
        page = client.get("/admin/ui")
        assert page.status_code == 200
        assert "unavailable" in page.text
    finally:
        container.adapter = original


def test_logout_everywhere_while_logged_out_redirects_to_login(client) -> None:
    # No session -> redirect, no CSRF check exercised yet.
    response = client.post("/admin/logout-everywhere", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_logout_flow_end_to_end(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/logout",
        data={"csrf_token": admin_ui_session},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # After logout the dashboard redirects back to login.
    page = client.get("/admin/ui", follow_redirects=False)
    assert page.status_code == 303


def test_mapping_page_renders(client, admin_ui_session) -> None:
    page = client.get("/admin/ui/mapping")
    assert page.status_code == 200
    assert "Mapping" in page.text


def test_keys_page_renders(client, admin_ui_session) -> None:
    page = client.get("/admin/ui/keys")
    assert page.status_code == 200
    assert "API keys" in page.text


def test_login_rejects_bad_credentials(client) -> None:
    response = client.post("/admin/login", data={"api_key": "wrong"}, follow_redirects=False)
    assert response.status_code == 401
    assert "Invalid" in response.text
