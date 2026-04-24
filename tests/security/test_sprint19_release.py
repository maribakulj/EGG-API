"""Sprint 19 regression tests: release 2.0 plumbing.

Covers:

- ``app.__version__`` and pyproject / briefcase metadata all agree
  on the 2.0.0 bump;
- ``GET /admin/v1/releases`` is admin-gated, reports the running
  version, and degrades gracefully when the GitHub poll fails or
  is disabled;
- the cache invalidates properly between calls so tests are not
  order-dependent;
- the packaged landing page exists with the expected structure.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.admin_api import releases as releases_mod

try:
    import tomllib as _toml  # Python 3.11+
except ImportError:  # pragma: no cover - 3.10 fallback
    import tomli as _toml  # type: ignore[import-not-found]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _toml.loads((_REPO_ROOT / "pyproject.toml").read_text())


# ---------------------------------------------------------------------------
# Version metadata agreement
# ---------------------------------------------------------------------------


def test_app_version_is_two_oh() -> None:
    assert __version__ == "2.0.0"


def test_pyproject_version_matches() -> None:
    assert _PYPROJECT["project"]["version"] == "2.0.0"


def test_briefcase_version_matches() -> None:
    assert _PYPROJECT["tool"]["briefcase"]["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# /admin/v1/releases
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drop_release_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    releases_mod._invalidate_cache()
    # Isolate the release check from the real GitHub — no test should
    # make a network call. Individual tests that want to assert remote
    # behaviour override ``_fetch_latest_from_github`` explicitly.
    monkeypatch.setattr(releases_mod, "_fetch_latest_from_github", lambda *a, **kw: None)


def test_releases_requires_admin(client: TestClient) -> None:
    resp = client.get("/admin/v1/releases")
    assert resp.status_code == 401


def test_releases_reports_current_version_when_upstream_unreachable(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.get("/admin/v1/releases", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_version"] == "2.0.0"
    assert body["latest_version"] is None
    assert body["update_available"] is False
    assert "python" in body
    assert "platform" in body


def test_releases_surfaces_upstream_payload(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        releases_mod,
        "_fetch_latest_from_github",
        lambda *a, **kw: {
            "latest_version": "2.0.1",
            "html_url": "https://example.org/release/v2.0.1",
            "published_at": "2026-05-01T09:00:00Z",
            "assets": [
                {
                    "name": "egg-api-2.0.1.msi",
                    "browser_download_url": "https://example.org/file.msi",
                    "size": 1234,
                }
            ],
        },
    )
    releases_mod._invalidate_cache()
    body = client.get("/admin/v1/releases", headers=admin_headers).json()
    assert body["latest_version"] == "2.0.1"
    assert body["update_available"] is True
    assert body["assets"][0]["name"] == "egg-api-2.0.1.msi"


def test_releases_honours_disable_env(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EGG_DISABLE_RELEASE_CHECK", "1")

    # With the check disabled the fetcher must not run even when
    # reachable; assert by setting a fetcher that would raise.
    def _fail(*a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("release check should be skipped")

    monkeypatch.setattr(releases_mod, "_fetch_latest_from_github", _fail)
    releases_mod._invalidate_cache()
    body = client.get("/admin/v1/releases", headers=admin_headers).json()
    assert body["release_check_disabled"] is True
    assert body["latest_version"] is None


def _real_fetch():
    """Bypass the autouse monkeypatch that stubs the upstream call."""
    from importlib import import_module, reload

    return reload(import_module("app.admin_api.releases"))._fetch_latest_from_github


def test_fetch_helper_returns_none_on_non_200() -> None:
    fetch = _real_fetch()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    assert fetch("x/y", client=client) is None


def test_fetch_helper_parses_payload_on_200() -> None:
    fetch = _real_fetch()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tag_name": "v2.1.0",
                "html_url": "https://example.org/release",
                "published_at": "2026-06-01T00:00:00Z",
                "assets": [{"name": "a.msi", "browser_download_url": "u", "size": 1}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    out = fetch("x/y", client=client)
    assert out is not None
    assert out["latest_version"] == "2.1.0"
    assert out["assets"][0]["name"] == "a.msi"


# ---------------------------------------------------------------------------
# Landing page artefacts
# ---------------------------------------------------------------------------


def test_landing_page_exists() -> None:
    site = _REPO_ROOT / "docs" / "site"
    assert (site / "index.html").exists()
    assert (site / "assets" / "site.css").exists()
    html = (site / "index.html").read_text()
    assert "EGG-API" in html
    assert "Download" in html
