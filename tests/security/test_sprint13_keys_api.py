"""Sprint 13 regression tests: REST CRUD for API keys (SPECS §13.7-13.10).

Focus:
- endpoints respond under ``/admin/v1/keys``;
- the raw secret is disclosed exactly once (on create and on rotate);
- stored state transitions correctly (list, patch, delete);
- admin-auth is enforced and invalid labels return structured errors;
- UI and API share a single service: a key created via REST shows up
  in the UI, and a key suspended via REST invalidates the UI session
  that was using it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create(client: TestClient, headers: dict[str, str], key_id: str) -> dict:
    resp = client.post("/admin/v1/keys", json={"key_id": key_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Authn
# ---------------------------------------------------------------------------


def test_keys_endpoints_require_admin_key(client: TestClient) -> None:
    for method, path in (
        ("get", "/admin/v1/keys"),
        ("post", "/admin/v1/keys"),
        ("get", "/admin/v1/keys/foo"),
        ("patch", "/admin/v1/keys/foo"),
        ("delete", "/admin/v1/keys/foo"),
    ):
        resp = client.request(method, path)
        assert resp.status_code == 401, (method, path, resp.text)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_returns_secret_exactly_once(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    created = _create(client, admin_headers, "partner_a")
    assert set(created.keys()) == {"key_id", "key", "created_at", "prefix"}
    # Exactly the raw secret prefix that the list endpoint exposes.
    assert created["prefix"] == created["key"][:8]
    # Listing never leaks the secret back out.
    listed = client.get("/admin/v1/keys", headers=admin_headers).json()
    row = next(k for k in listed["keys"] if k["key_id"] == "partner_a")
    assert "key" not in row
    assert row["status"] == "active"


def test_create_rejects_bad_label(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post("/admin/v1/keys", json={"key_id": "bad label!"}, headers=admin_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_parameter"


def test_create_rejects_duplicate_label(client: TestClient, admin_headers: dict[str, str]) -> None:
    _create(client, admin_headers, "dup")
    resp = client.post("/admin/v1/keys", json={"key_id": "dup"}, headers=admin_headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_create_rejects_extra_body_fields(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    # extra="forbid" on CreateKeyRequest must surface typos as a 422.
    resp = client.post(
        "/admin/v1/keys",
        json={"key_id": "ok", "note": "oops"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List / Get
# ---------------------------------------------------------------------------


def test_list_keys_includes_bootstrap_admin(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    listed = client.get("/admin/v1/keys", headers=admin_headers).json()
    labels = {k["key_id"] for k in listed["keys"]}
    assert "admin" in labels


def test_get_unknown_key_returns_404(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/admin/v1/keys/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Patch: activate / suspend / revoke / rotate
# ---------------------------------------------------------------------------


def test_patch_transitions_status(client: TestClient, admin_headers: dict[str, str]) -> None:
    _create(client, admin_headers, "partner_b")
    for action, expected in (
        ("suspend", "suspended"),
        ("activate", "active"),
        ("revoke", "revoked"),
    ):
        resp = client.patch(
            "/admin/v1/keys/partner_b",
            json={"action": action},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == expected


def test_patch_rotate_returns_new_secret(client: TestClient, admin_headers: dict[str, str]) -> None:
    created = _create(client, admin_headers, "partner_c")
    resp = client.patch(
        "/admin/v1/keys/partner_c",
        json={"action": "rotate"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key_id"] == "partner_c"
    assert body["key"] != created["key"]
    assert body["key"]  # non-empty


def test_patch_unknown_action_is_rejected(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    _create(client, admin_headers, "partner_d")
    resp = client.patch(
        "/admin/v1/keys/partner_d",
        json={"action": "purge"},
        headers=admin_headers,
    )
    # Literal type on the Pydantic body => 422 from FastAPI.
    assert resp.status_code == 422


def test_patch_on_unknown_key_returns_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.patch(
        "/admin/v1/keys/nope",
        json={"action": "suspend"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_is_soft_revoke(client: TestClient, admin_headers: dict[str, str]) -> None:
    _create(client, admin_headers, "partner_e")
    resp = client.delete("/admin/v1/keys/partner_e", headers=admin_headers)
    assert resp.status_code == 204
    # Still listable, but now revoked (audit trail intact).
    row = client.get("/admin/v1/keys/partner_e", headers=admin_headers).json()
    assert row["status"] == "revoked"


def test_delete_unknown_returns_404(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.delete("/admin/v1/keys/missing", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# UI <-> API share the same source of truth
# ---------------------------------------------------------------------------


def test_key_created_via_api_appears_in_ui(
    client: TestClient, admin_headers: dict[str, str], admin_ui_session: str
) -> None:
    _create(client, admin_headers, "visible_in_ui")
    page = client.get("/admin/ui/keys")
    assert page.status_code == 200
    assert "visible_in_ui" in page.text


def test_api_revoke_invalidates_ui_session(
    client: TestClient, admin_headers: dict[str, str], admin_ui_session: str
) -> None:
    # The conftest admin_ui_session fixture has already issued a cookie
    # bound to key_id=="admin". Revoking admin through the REST API must
    # kick that session.
    before = client.get("/admin/ui")
    assert before.status_code == 200
    resp = client.patch("/admin/v1/keys/admin", json={"action": "suspend"}, headers=admin_headers)
    assert resp.status_code == 200
    after = client.get("/admin/ui", follow_redirects=False)
    assert after.status_code == 303  # bounced back to /admin/login
