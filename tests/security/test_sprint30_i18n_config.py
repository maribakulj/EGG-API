"""Sprint 30 regression tests: deployment-wide language picker.

Covers:
- ``AppConfig.default_language`` validates the Literal["en", "fr"] | None;
- Resolver picks up ``config.default_language`` after cookie/header but
  before env/default — and never overrides the visitor's own pick;
- Wizard landing page renders a language picker; posting to
  ``/admin/ui/setup/language`` writes the config **and** sets the
  ``egg_lang`` cookie;
- Unsupported language POSTs are refused (400);
- Admin config form exposes a ``default_language`` dropdown; saving it
  round-trips through ``/admin/ui/config``;
- Admin shell + imports page render French labels when the config
  default is ``fr`` and no other signal is present.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config.models import AppConfig
from app.dependencies import container
from app.i18n import LANG_COOKIE

# ---------------------------------------------------------------------------
# AppConfig.default_language
# ---------------------------------------------------------------------------


def test_app_config_default_language_defaults_to_none() -> None:
    cfg = AppConfig()
    assert cfg.default_language is None


def test_app_config_default_language_accepts_en_fr() -> None:
    cfg_en = AppConfig.model_validate({"default_language": "en"})
    cfg_fr = AppConfig.model_validate({"default_language": "fr"})
    assert cfg_en.default_language == "en"
    assert cfg_fr.default_language == "fr"


def test_app_config_default_language_rejects_garbage() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"default_language": "de"})


# ---------------------------------------------------------------------------
# Resolver wiring
# ---------------------------------------------------------------------------


def _set_config_default(lang: str | None) -> None:
    """Mutate the in-memory config without invoking ``container.reload``.

    Reloading rebuilds every piece of state (store, api_keys, policy,
    rate limiters…) which invalidates the test fixture's admin session.
    For test purposes the in-memory mutation is equivalent since the
    resolver reads ``container.config_manager.config.default_language``
    directly.
    """

    cfg = container.config_manager.config.model_copy(deep=True)
    cfg.default_language = lang  # type: ignore[assignment]
    container.config_manager._config = cfg


def test_resolver_uses_config_default_language_as_fallback(
    client: TestClient,
) -> None:
    _set_config_default("fr")
    # No query, no cookie, no Accept-Language → config default wins.
    resp = client.get("/")
    assert 'html lang="fr"' in resp.text


def test_resolver_visitor_cookie_beats_config_default(client: TestClient) -> None:
    _set_config_default("fr")
    client.cookies.set(LANG_COOKIE, "en")
    try:
        resp = client.get("/")
        assert 'html lang="en"' in resp.text
    finally:
        client.cookies.clear()


def test_resolver_header_beats_config_default(client: TestClient) -> None:
    _set_config_default("fr")
    resp = client.get("/", headers={"accept-language": "en-US"})
    assert 'html lang="en"' in resp.text


def test_resolver_config_default_none_defers_to_env(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _set_config_default(None)
    monkeypatch.setenv("EGG_DEFAULT_LANG", "fr")
    resp = client.get("/")
    assert 'html lang="fr"' in resp.text


# ---------------------------------------------------------------------------
# Wizard landing picker
# ---------------------------------------------------------------------------


def test_wizard_landing_shows_language_picker(client: TestClient, admin_ui_session: str) -> None:
    resp = client.get("/admin/ui/setup")
    assert resp.status_code == 200
    body = resp.text
    assert 'action="/admin/ui/setup/language"' in body
    assert 'value="en"' in body
    assert 'value="fr"' in body


def test_wizard_language_post_persists_config_and_sets_cookie(
    client: TestClient, admin_ui_session: str
) -> None:
    resp = client.post(
        "/admin/ui/setup/language",
        data={"csrf_token": admin_ui_session, "lang": "fr"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup"
    assert resp.cookies.get(LANG_COOKIE) == "fr"
    assert container.config_manager.config.default_language == "fr"


def test_wizard_language_post_rejects_unsupported_lang(
    client: TestClient, admin_ui_session: str
) -> None:
    resp = client.post(
        "/admin/ui/setup/language",
        data={"csrf_token": admin_ui_session, "lang": "de"},
    )
    assert resp.status_code == 400
    assert container.config_manager.config.default_language is None


def test_wizard_language_requires_csrf(client: TestClient, admin_ui_session: str) -> None:
    # CSRF enforcement returns a 403 page; the exact status may differ
    # across versions — we only care that the config wasn't mutated.
    client.post("/admin/ui/setup/language", data={"lang": "fr"})
    assert container.config_manager.config.default_language is None


# ---------------------------------------------------------------------------
# /admin/ui/config language dropdown
# ---------------------------------------------------------------------------


def test_admin_config_form_shows_language_dropdown(
    client: TestClient, admin_ui_session: str
) -> None:
    resp = client.get("/admin/ui/config")
    assert resp.status_code == 200
    body = resp.text
    assert 'name="default_language"' in body
    assert ">English<" in body
    assert ">Français<" in body


def test_admin_config_save_persists_default_language(
    client: TestClient, admin_ui_session: str
) -> None:
    cfg = container.config_manager.config
    profile = cfg.profiles[cfg.security_profile]
    resp = client.post(
        "/admin/ui/config",
        data={
            "csrf_token": admin_ui_session,
            "backend_url": cfg.backend.url or "http://example:9200",
            "backend_index": cfg.backend.index or "records",
            "security_profile": cfg.security_profile,
            "public_mode": cfg.auth.public_mode,
            "sqlite_path": cfg.storage.sqlite_path,
            "allow_empty_query": "false",
            "page_size_default": str(profile.page_size_default),
            "page_size_max": str(profile.page_size_max),
            "max_depth": str(profile.max_depth),
            "default_language": "fr",
        },
    )
    assert resp.status_code == 200
    assert container.config_manager.config.default_language == "fr"


def test_admin_config_save_with_empty_language_clears_value(
    client: TestClient, admin_ui_session: str
) -> None:
    _set_config_default("fr")
    cfg = container.config_manager.config
    profile = cfg.profiles[cfg.security_profile]
    resp = client.post(
        "/admin/ui/config",
        data={
            "csrf_token": admin_ui_session,
            "backend_url": cfg.backend.url or "http://example:9200",
            "backend_index": cfg.backend.index or "records",
            "security_profile": cfg.security_profile,
            "public_mode": cfg.auth.public_mode,
            "sqlite_path": cfg.storage.sqlite_path,
            "allow_empty_query": "false",
            "page_size_default": str(profile.page_size_default),
            "page_size_max": str(profile.page_size_max),
            "max_depth": str(profile.max_depth),
            "default_language": "",
        },
    )
    assert resp.status_code == 200
    assert container.config_manager.config.default_language is None


# ---------------------------------------------------------------------------
# Admin shell + imports render in FR when config default is FR
# ---------------------------------------------------------------------------


def test_admin_shell_nav_renders_in_french_via_config_default(
    client: TestClient, admin_ui_session: str
) -> None:
    _set_config_default("fr")
    resp = client.get("/admin/ui")
    assert resp.status_code == 200
    body = resp.text
    assert 'html lang="fr"' in body
    assert "Assistant de configuration" in body
    assert "Imports de données" in body


def test_admin_imports_page_renders_in_french_via_config_default(
    client: TestClient, admin_ui_session: str
) -> None:
    _set_config_default("fr")
    resp = client.get("/admin/ui/imports")
    assert resp.status_code == 200
    body = resp.text
    # Page heading + key labels. Jinja auto-escapes apostrophes so we
    # look for the escape-safe slice rather than the raw French phrase.
    assert "Imports de données" in body
    assert "Ajouter une source d" in body
    assert "Toutes les heures" in body  # schedule option
    assert "Libellé" in body  # first form label


def test_admin_lang_switch_link_round_trips(client: TestClient, admin_ui_session: str) -> None:
    # From English (default), the nav shows a ?lang=fr link.
    resp = client.get("/admin/ui")
    assert 'href="?lang=fr"' in resp.text
    # After ?lang=fr, the nav shows a ?lang=en link.
    resp = client.get("/admin/ui?lang=fr")
    assert 'href="?lang=en"' in resp.text
    assert 'html lang="fr"' in resp.text
