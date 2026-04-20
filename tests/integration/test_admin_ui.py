from __future__ import annotations

from app.dependencies import container


def _login(client):
    return client.post(
        "/admin/login",
        data={"api_key": container.api_keys.default_admin_key},
        follow_redirects=False,
    )


def test_ui_routes_protected(client) -> None:
    response = client.get("/admin/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_dashboard_loads_when_authenticated(client) -> None:
    login = _login(client)
    assert login.status_code == 303
    page = client.get("/admin/ui")
    assert page.status_code == 200
    assert "Dashboard" in page.text


def test_config_page_shows_current_config(client) -> None:
    _login(client)
    page = client.get("/admin/ui/config")
    assert page.status_code == 200
    assert container.config_manager.config.backend.url in page.text


def test_config_update_valid_flow(client, admin_ui_session) -> None:
    cfg = container.config_manager.config
    profile = cfg.profiles[cfg.security_profile]
    response = client.post(
        "/admin/ui/config",
        data={
            "csrf_token": admin_ui_session,
            "backend_url": "http://example.org:9200",
            "backend_index": "newindex",
            "security_profile": cfg.security_profile,
            "public_mode": cfg.auth.public_mode,
            "sqlite_path": cfg.storage.sqlite_path,
            "allow_empty_query": "false",
            "page_size_default": str(profile.page_size_default),
            "page_size_max": str(profile.page_size_max),
            "max_depth": str(profile.max_depth),
        },
    )
    assert response.status_code == 200
    assert "Configuration saved successfully" in response.text


def test_config_update_invalid_rejected(client, admin_ui_session) -> None:
    cfg = container.config_manager.config
    profile = cfg.profiles[cfg.security_profile]
    response = client.post(
        "/admin/ui/config",
        data={
            "csrf_token": admin_ui_session,
            "backend_url": "http://example.org:9200",
            "backend_index": "x",
            "security_profile": "not-a-profile",
            "public_mode": cfg.auth.public_mode,
            "sqlite_path": cfg.storage.sqlite_path,
            "allow_empty_query": "false",
            "page_size_default": str(profile.page_size_default),
            "page_size_max": str(profile.page_size_max),
            "max_depth": str(profile.max_depth),
        },
    )
    assert response.status_code == 400
    assert "Unable to save configuration" in response.text


def test_api_key_create_and_suspend_flow(client, admin_ui_session) -> None:
    created = client.post(
        "/admin/ui/keys/create",
        data={"key_id": "ui-test-key", "csrf_token": admin_ui_session},
    )
    assert created.status_code == 200
    assert "Copy it now" in created.text

    action = client.post(
        "/admin/ui/keys/ui-test-key/status",
        data={"action": "suspend", "csrf_token": admin_ui_session},
        follow_redirects=False,
    )
    assert action.status_code == 303

    page = client.get("/admin/ui/keys")
    assert "ui-test-key" in page.text
    assert "suspended" in page.text


def test_usage_page_renders_recent_data(client) -> None:
    _login(client)
    client.get("/v1/search?q=hello")
    page = client.get("/admin/ui/usage")
    assert page.status_code == 200
    assert "Recent activity" in page.text
    assert "/v1/search" in page.text
