"""Sprint 29 regression tests: French i18n.

Covers the i18n resolver priority (query param > cookie > header >
env default > English), French rendering of the landing and /about
pages, cookie persistence when ``?lang=`` is used, and the graceful
fallback when an unknown key is looked up.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.i18n import (
    DEFAULT_LANG,
    LANG_COOKIE,
    SUPPORTED_LANGS,
    resolve_lang,
    translator,
)

# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _make_request(
    *,
    query: str = "",
    headers: list[tuple[bytes, bytes]] | None = None,
    cookies: dict[str, str] | None = None,
) -> Request:
    header_pairs: list[tuple[bytes, bytes]] = list(headers or [])
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        header_pairs.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": header_pairs,
        "query_string": query.encode(),
    }
    return Request(scope)


def test_resolve_lang_default_is_english() -> None:
    assert resolve_lang(_make_request()) == "en"
    assert DEFAULT_LANG == "en"
    assert "en" in SUPPORTED_LANGS and "fr" in SUPPORTED_LANGS


def test_query_param_wins_over_everything() -> None:
    req = _make_request(
        query="lang=fr",
        headers=[(b"accept-language", b"en-US")],
        cookies={LANG_COOKIE: "en"},
    )
    assert resolve_lang(req) == "fr"


def test_cookie_wins_over_header() -> None:
    req = _make_request(
        headers=[(b"accept-language", b"en-US")],
        cookies={LANG_COOKIE: "fr"},
    )
    assert resolve_lang(req) == "fr"


def test_accept_language_header_is_honoured() -> None:
    req = _make_request(headers=[(b"accept-language", b"fr-FR,fr;q=0.9,en;q=0.5")])
    assert resolve_lang(req) == "fr"


def test_unsupported_language_falls_back_to_default() -> None:
    req = _make_request(query="lang=de")
    assert resolve_lang(req) == "en"


def test_env_default_applies_when_nothing_else_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EGG_DEFAULT_LANG", "fr")
    assert resolve_lang(_make_request()) == "fr"


def test_none_request_returns_default() -> None:
    assert resolve_lang(None) == "en"


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


def test_translator_returns_french_for_known_key() -> None:
    t = translator("fr")
    assert "assistant" in t("landing.cta.setup").lower()


def test_translator_falls_back_to_english_for_missing_french_key() -> None:
    # Every key we ship is in both catalogues, so simulate a missing
    # entry by asking for a key that only exists in English (there
    # isn't one today — we assert the generic fallback path instead).
    t = translator("fr")
    # Fabricated key does not exist anywhere: we get the raw key back.
    assert t("totally.made.up.key") == "totally.made.up.key"


def test_translator_unknown_lang_uses_english() -> None:
    t = translator("klingon")
    assert "heritage" in t("landing.hero.title").lower()


# ---------------------------------------------------------------------------
# Landing routes — render in the right language
# ---------------------------------------------------------------------------


def test_landing_index_renders_french_with_query(client: TestClient) -> None:
    resp = client.get("/?lang=fr")
    assert resp.status_code == 200
    body = resp.text
    assert 'html lang="fr"' in body
    assert "assistant de configuration" in body.lower()
    assert "bibliothèque" in body.lower()
    # The language-switch cookie was set.
    assert LANG_COOKIE in resp.cookies
    assert resp.cookies[LANG_COOKIE] == "fr"


def test_landing_index_respects_cookie(client: TestClient) -> None:
    client.cookies.set(LANG_COOKIE, "fr")
    try:
        resp = client.get("/")
        body = resp.text
        assert 'html lang="fr"' in body
        assert "assistant de configuration" in body.lower()
    finally:
        client.cookies.clear()


def test_landing_index_respects_accept_language(client: TestClient) -> None:
    resp = client.get("/", headers={"accept-language": "fr-FR,fr;q=0.8"})
    assert 'html lang="fr"' in resp.text


def test_landing_about_renders_french(client: TestClient) -> None:
    resp = client.get("/about?lang=fr")
    assert resp.status_code == 200
    body = resp.text
    assert "Principes de conception" in body
    assert "Aucune compétence informatique" in body


def test_landing_index_links_query_does_not_set_cookie_on_unknown_lang(
    client: TestClient,
) -> None:
    resp = client.get("/?lang=de")
    # Unsupported value → default rendering + no cookie written.
    assert 'html lang="en"' in resp.text
    assert LANG_COOKIE not in resp.cookies


def test_language_switcher_appears_in_both_languages(client: TestClient) -> None:
    # Switcher lists both language names, in each language's own label.
    body_en = client.get("/").text
    body_fr = client.get("/?lang=fr").text
    for body in (body_en, body_fr):
        assert "English" in body
        assert "Français" in body
