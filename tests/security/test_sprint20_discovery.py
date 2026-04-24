"""Sprint 20 regression tests: backend auto-discovery.

The wizard's step 1 now has a "Detect a backend" button that probes
the conventional loopback + docker-compose hosts in parallel. These
tests cover:

- the pure helper ``discover_backend_candidates`` with a
  ``httpx.MockTransport`` simulating every realistic backend reply
  (Elasticsearch root, OpenSearch root, HTTP 401, HTTP 500, a non-
  search server, a connection error);
- ``EGG_DISCOVERY_HOSTS`` extends the default allowlist;
- the admin UI routes ``POST /admin/ui/setup/discover`` and
  ``POST /admin/ui/setup/discover/use`` honour login + CSRF, render
  the candidate table and persist the chosen URL into the draft.

Network is never contacted — every probe is routed through the
MockTransport fixture.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.admin_ui import setup_service as setup_mod
from app.admin_ui.setup_service import (
    DiscoveryCandidate,
    _env_discovery_hosts,
    _interpret_probe,
    discover_backend_candidates,
)
from app.dependencies import container

# ---------------------------------------------------------------------------
# Pure probe interpretation
# ---------------------------------------------------------------------------


def _fake_response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {})


def test_interpret_recognises_elasticsearch_root() -> None:
    resp = _fake_response(
        200,
        {
            "version": {"number": "8.4.2"},
            "tagline": "You Know, for Search",
        },
    )
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "ok"
    assert cand.backend_type == "elasticsearch"
    assert cand.version == "8.4.2"


def test_interpret_recognises_opensearch_root() -> None:
    resp = _fake_response(
        200,
        {
            "version": {"number": "2.9.0", "distribution": "opensearch"},
            "tagline": "The OpenSearch Project",
        },
    )
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "ok"
    assert cand.backend_type == "opensearch"
    assert cand.version == "2.9.0"


def test_interpret_flags_auth_required_as_usable() -> None:
    # Elasticsearch 8+ with security enabled answers 401 on the root.
    # The wizard still wants to surface the URL so the operator can
    # paste credentials on the next step.
    resp = _fake_response(401, {"error": "security_exception"})
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "needs_auth"
    assert cand.backend_type is None
    assert "authentication" in (cand.message or "").lower()


def test_interpret_rejects_non_search_server() -> None:
    resp = _fake_response(200, {"service": "some other app"})
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "unreachable"
    assert cand.backend_type is None


def test_interpret_rejects_unsupported_version() -> None:
    resp = _fake_response(
        200,
        {"version": {"number": "6.8.3"}, "tagline": "You Know, for Search"},
    )
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "unsupported_version"
    assert cand.version == "6.8.3"


def test_interpret_rejects_non_2xx_but_not_401() -> None:
    resp = _fake_response(500)
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "unreachable"


def test_interpret_rejects_non_json_body() -> None:
    # Simulate a raw TCP listener that returns HTML.
    request = httpx.Request("GET", "http://localhost:9200")
    resp = httpx.Response(200, content=b"<html>nope</html>", request=request)
    cand = _interpret_probe("http://localhost:9200", resp)
    assert cand.status == "unreachable"


# ---------------------------------------------------------------------------
# End-to-end discovery with MockTransport
# ---------------------------------------------------------------------------


def _build_mock_transport() -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port
        if host in {"localhost", "127.0.0.1"} and port == 9200:
            return httpx.Response(
                200,
                json={"version": {"number": "8.5.0"}, "tagline": "You Know, for Search"},
            )
        if host == "opensearch" and port == 9200:
            return httpx.Response(
                200,
                json={
                    "version": {"number": "2.11.0", "distribution": "opensearch"},
                    "tagline": "The OpenSearch Project",
                },
            )
        if host == "elasticsearch" and port == 9200:
            return httpx.Response(401)
        # Everything else: the kernel would have refused the
        # connection, which httpx surfaces as ConnectError.
        raise httpx.ConnectError("refused", request=request)

    return httpx.MockTransport(_handler)


def test_discover_returns_all_probes_in_order() -> None:
    client = httpx.Client(transport=_build_mock_transport())
    try:
        results = discover_backend_candidates(
            urls=[
                "http://localhost:9200",
                "http://opensearch:9200",
                "http://elasticsearch:9200",
                "http://nope:9200",
            ],
            timeout_seconds=0.5,
            client=client,
        )
    finally:
        client.close()
    assert {r.url for r in results} == {
        "http://localhost:9200",
        "http://opensearch:9200",
        "http://elasticsearch:9200",
        "http://nope:9200",
    }
    statuses = {r.url: r.status for r in results}
    assert statuses["http://localhost:9200"] == "ok"
    assert statuses["http://opensearch:9200"] == "ok"
    assert statuses["http://elasticsearch:9200"] == "needs_auth"
    assert statuses["http://nope:9200"] == "unreachable"


def test_env_discovery_hosts_extends_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_DISCOVERY_HOSTS", "es-staging.internal:9200 , other")
    parsed = _env_discovery_hosts()
    assert ("es-staging.internal", 9200) in parsed
    # Bare hostname without port defaults to 9200.
    assert ("other", 9200) in parsed


def test_env_discovery_hosts_ignores_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_DISCOVERY_HOSTS", "only:abc,,")
    # "only:abc" has a bad port -> dropped; empty entries -> dropped.
    assert _env_discovery_hosts() == []


def test_discover_no_urls_returns_empty() -> None:
    assert discover_backend_candidates(urls=[]) == []


# ---------------------------------------------------------------------------
# /admin/ui/setup/discover routes
# ---------------------------------------------------------------------------


def test_discover_route_requires_login(client: TestClient) -> None:
    resp = client.post("/admin/ui/setup/discover", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_discover_route_renders_candidates(
    client: TestClient, admin_ui_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = [
        DiscoveryCandidate(
            url="http://localhost:9200",
            backend_type="elasticsearch",
            version="8.5.0",
            status="ok",
        ),
        DiscoveryCandidate(
            url="http://elasticsearch:9200",
            backend_type=None,
            version=None,
            status="needs_auth",
            message="Backend needs auth",
        ),
    ]
    # Patch the symbol referenced by the routes module (early binding).
    import app.admin_ui.routes as routes_mod

    monkeypatch.setattr(routes_mod, "discover_backend_candidates", lambda: fake)
    resp = client.post(
        "/admin/ui/setup/discover",
        data={"csrf_token": admin_ui_session},
    )
    assert resp.status_code == 200
    assert "http://localhost:9200" in resp.text
    assert "needs auth" in resp.text
    assert "Found 1 reachable" in resp.text


def test_discover_route_reports_no_backends(
    client: TestClient, admin_ui_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.admin_ui.routes as routes_mod

    monkeypatch.setattr(routes_mod, "discover_backend_candidates", lambda: [])
    resp = client.post(
        "/admin/ui/setup/discover",
        data={"csrf_token": admin_ui_session},
    )
    assert resp.status_code == 200
    assert "No backend answered" in resp.text


def test_discover_use_route_patches_draft(client: TestClient, admin_ui_session: str) -> None:
    container.store.save_setup_draft(
        "admin",
        setup_mod.SetupDraft().to_json(),
        "backend",
    )
    resp = client.post(
        "/admin/ui/setup/discover/use",
        data={
            "csrf_token": admin_ui_session,
            "url": "http://localhost:9200",
            "backend_type": "opensearch",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup/backend"
    payload, step = container.store.load_setup_draft("admin")  # type: ignore[misc]
    assert step == "backend"
    assert payload["backend"]["url"] == "http://localhost:9200"
    assert payload["backend"]["type"] == "opensearch"


def test_discover_use_route_rejects_bad_type(client: TestClient, admin_ui_session: str) -> None:
    resp = client.post(
        "/admin/ui/setup/discover/use",
        data={
            "csrf_token": admin_ui_session,
            "url": "http://localhost:9200",
            "backend_type": "redis",  # not allowed
        },
        follow_redirects=False,
    )
    # Unknown type silently bounces back to the form.
    assert resp.status_code == 303


def test_discover_use_rejects_empty_url(client: TestClient, admin_ui_session: str) -> None:
    resp = client.post(
        "/admin/ui/setup/discover/use",
        data={"csrf_token": admin_ui_session, "url": "", "backend_type": "elasticsearch"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
