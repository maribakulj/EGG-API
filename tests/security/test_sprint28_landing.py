"""Sprint 28 regression tests: public landing page + /about.

Covers:
- ``GET /`` returns HTML with the expected positioning copy (three
  collection profiles, nine importers, CTA to the wizard);
- ``GET /about`` returns the positioning details page;
- The landing status tile gracefully degrades when the backend's
  ``detect()`` raises — non-technical operators still see a useful
  page even when ES is down;
- Static assets mounted at ``/landing-static/landing.css`` are served
  and carry a CSS content-type;
- Landing routes do not leak into the public OpenAPI (``include_in_schema=False``)
  so the Sprint 5 path snapshot stays stable;
- The landing page exposes the expected outbound links (admin console,
  setup wizard, OAI endpoint, OpenAPI) so the marketing copy matches
  the deployed surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.dependencies import container

# ---------------------------------------------------------------------------
# /
# ---------------------------------------------------------------------------


def test_landing_index_renders_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # Positioning copy markers.
    assert "heritage API" in body
    assert "setup wizard" in body.lower()
    # Three profiles are listed.
    assert "Library" in body
    assert "Museum" in body
    assert "Archive" in body
    # Nine importers mentioned.
    assert "OAI-PMH" in body
    assert "LIDO" in body
    assert "MARC" in body
    assert "EAD" in body
    assert "CSV" in body


def test_landing_index_links_point_at_real_surfaces(client: TestClient) -> None:
    body = client.get("/").text
    # CTA links that must exist elsewhere in the app.
    assert 'href="/admin/ui"' in body
    assert 'href="/admin/ui/setup"' in body
    assert 'href="/v1/oai?verb=Identify"' in body
    assert 'href="/v1/openapi.json"' in body
    assert 'href="/v1/search"' in body


def test_landing_status_tile_shows_ok_when_backend_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        container.adapter,
        "detect",
        lambda: {"flavor": "elasticsearch", "version": "8.10.0"},
    )
    body = client.get("/").text
    assert "status-pill ok" in body
    assert "elasticsearch 8.10.0" in body or "elasticsearch" in body


def test_landing_status_tile_degrades_when_backend_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> dict:
        raise RuntimeError("backend offline")

    monkeypatch.setattr(container.adapter, "detect", _boom)
    body = client.get("/").text
    # The page still renders; the backend pill flips to warn.
    assert "status-pill warn" in body
    assert "unreachable" in body


# ---------------------------------------------------------------------------
# /about
# ---------------------------------------------------------------------------


def test_about_page_renders(client: TestClient) -> None:
    resp = client.get("/about")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Design principles" in body
    assert "Zero IT required" in body


# ---------------------------------------------------------------------------
# Static assets + OpenAPI exclusion
# ---------------------------------------------------------------------------


def test_landing_static_css_is_served(client: TestClient) -> None:
    resp = client.get("/landing-static/landing.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert ".topbar" in resp.text
    assert ".cta" in resp.text


def test_landing_routes_are_hidden_from_public_openapi(client: TestClient) -> None:
    spec = client.get("/v1/openapi.json").json()
    paths = set(spec.get("paths", {}))
    assert "/" not in paths
    assert "/about" not in paths
