"""Regression tests for Vague 3 (H6-H8, M3-M7): API quality & validation."""

from __future__ import annotations

import pytest

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.config.models import AppConfig
from app.dependencies import container
from app.mappers.schema_mapper import SchemaMapper, _parse_iso_date, _safe_public_url
from app.schemas.query import NormalizedQuery

# ---------------------------------------------------------------------------
# H6 — Jinja2 autoescape
# ---------------------------------------------------------------------------


def test_h6_xss_payload_in_config_is_escaped(client, admin_headers) -> None:
    # Seed config with an attacker-supplied value, then render the page.
    cfg = container.config_manager.config
    cfg.backend.url = '"><script>alert(1)</script>'
    container.config_manager._config = cfg

    client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    response = client.get("/admin/ui/config")
    assert response.status_code == 200
    # Raw <script> must never appear; the escaped entity must.
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_h6_xss_payload_in_key_label_is_escaped(client, admin_headers) -> None:
    client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    # Inject via the key list: create a valid key, then tamper with its id
    # directly in the store to bypass H7 on creation and verify escaping.
    container.api_keys.create("benign-key")
    with __import__("sqlite3").connect(container.store.db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET key_id = ? WHERE key_id = ?",
            ("<img src=x onerror=alert(1)>", "benign-key"),
        )
        conn.commit()

    response = client.get("/admin/ui/keys")
    assert response.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


def test_h6_templates_env_autoescape_enabled() -> None:
    from app.admin_ui.routes import templates

    assert templates.env.autoescape is True


# ---------------------------------------------------------------------------
# H7 — key_id validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_label",
    [
        "",
        " ",
        "x" * 65,
        "has space",
        "inject;drop",
        "slash/path",
        "quote'\"",
        "<script>",
    ],
)
def test_h7_create_key_rejects_invalid_label(bad_label: str, client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/create",
        data={"key_id": bad_label, "csrf_token": admin_ui_session},
    )
    assert response.status_code == 400
    assert "must be 1-64 characters" in response.text


@pytest.mark.parametrize("label", ["abc", "team-01", "svc.prod", "api_key_1"])
def test_h7_create_key_accepts_valid_labels(label: str, client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/create",
        data={"key_id": label, "csrf_token": admin_ui_session},
    )
    assert response.status_code == 200
    assert "Copy it now" in response.text


def test_h7_status_action_rejects_invalid_path_param(client, admin_ui_session) -> None:
    response = client.post(
        "/admin/ui/keys/bad%20id/status",
        data={"action": "revoke", "csrf_token": admin_ui_session},
        follow_redirects=False,
    )
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# H8 — date_parser / url_passthrough hardening
# ---------------------------------------------------------------------------


def test_h8_date_parser_returns_none_for_garbage() -> None:
    assert _parse_iso_date("not a date") is None
    assert _parse_iso_date(None) is None
    assert _parse_iso_date("") is None
    assert _parse_iso_date(12345) is None


def test_h8_date_parser_handles_z_suffix() -> None:
    assert _parse_iso_date("2024-01-02T03:04:05Z") == "2024-01-02"
    assert _parse_iso_date("2024-01-02") == "2024-01-02"


def test_h8_safe_public_url_rejects_non_http_schemes() -> None:
    assert _safe_public_url("javascript:alert(1)") is None
    assert _safe_public_url("file:///etc/passwd") is None
    assert _safe_public_url("ftp://example.org/") is None
    assert _safe_public_url("http:///no-host") is None
    assert _safe_public_url("http://") is None
    assert _safe_public_url(None) is None
    assert _safe_public_url(42) is None


def test_h8_safe_public_url_accepts_valid_urls() -> None:
    assert _safe_public_url("https://example.org/x") == "https://example.org/x"
    assert _safe_public_url("http://example.org") == "http://example.org"


def test_h8_mapper_swallows_bad_dates() -> None:
    cfg = AppConfig()
    cfg.mapping = {
        "id": cfg.mapping["id"],
        "type": cfg.mapping["type"],
    }
    from app.config.models import FieldMapping

    cfg.mapping["published_at"] = FieldMapping(source="date_raw", mode="date_parser")
    mapper = SchemaMapper(cfg)
    rec = mapper.map_record({"id": "1", "type": "object", "date_raw": "???"})
    # The bad value becomes None rather than crashing the request.
    assert rec.id == "1"


# ---------------------------------------------------------------------------
# M3 — max_buckets_per_facet propagated into adapter
# ---------------------------------------------------------------------------


def test_m3_adapter_uses_profile_bucket_cap() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records", max_buckets_per_facet=7)
    body = adapter.translate_query(NormalizedQuery(q="x", facets=["type"]))
    assert body["aggs"]["type"]["terms"]["size"] == 7


def test_m3_explicit_override_wins_over_instance_cap() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records", max_buckets_per_facet=7)
    body = adapter.translate_query(
        NormalizedQuery(q="x", facets=["type"]),
        max_buckets_per_facet=3,
    )
    assert body["aggs"]["type"]["terms"]["size"] == 3


# ---------------------------------------------------------------------------
# M4 — raw_fields strips internal underscore keys
# ---------------------------------------------------------------------------


def test_m4_raw_fields_filter_strips_internal_keys() -> None:
    cfg = AppConfig()
    cfg.profiles[cfg.security_profile].allow_raw_fields = True
    mapper = SchemaMapper(cfg)
    rec = mapper.map_record(
        {
            "id": "42",
            "type": "object",
            "title": "Hello",
            "_id": "internal-id",
            "_score": 12.3,
            "_index": "records",
            "public_note": "ok",
        }
    )
    raw = rec.raw_fields
    assert raw is not None
    assert "_id" not in raw
    assert "_score" not in raw
    assert "_index" not in raw
    assert raw["public_note"] == "ok"


def test_m4_raw_fields_absent_when_profile_forbids() -> None:
    cfg = AppConfig()
    assert cfg.profiles[cfg.security_profile].allow_raw_fields is False
    mapper = SchemaMapper(cfg)
    rec = mapper.map_record({"id": "1", "type": "object"})
    assert rec.raw_fields is None


# ---------------------------------------------------------------------------
# M7 — public endpoints carry docstrings (surfaced in OpenAPI)
# ---------------------------------------------------------------------------


def test_m7_public_endpoints_have_openapi_descriptions(client) -> None:
    schema = client.get("/v1/openapi.json").json()
    paths = schema["paths"]
    for path in ("/v1/search", "/v1/facets", "/v1/records/{record_id}", "/v1/health"):
        ops = paths[path]
        get_op = ops.get("get", {})
        assert get_op.get("description") or get_op.get("summary"), (
            f"missing docstring-derived description on {path}"
        )


def test_m7_admin_endpoints_have_openapi_descriptions(client, admin_headers) -> None:
    # Admin paths are exposed only on the authenticated ``/admin/v1/openapi.json``;
    # the public ``/v1/openapi.json`` strips them so anonymous callers cannot
    # fingerprint the operator surface.
    schema = client.get("/admin/v1/openapi.json", headers=admin_headers).json()
    paths = schema["paths"]
    admin_get = paths["/admin/v1/config"].get("get", {})
    assert admin_get.get("description") or admin_get.get("summary")


def test_m7_public_openapi_strips_admin_paths(client) -> None:
    # Security contract: the public schema never carries /admin/* routes.
    schema = client.get("/v1/openapi.json").json()
    admin_paths = [p for p in schema["paths"] if p.startswith("/admin/")]
    assert admin_paths == [], f"public OpenAPI leaked admin paths: {admin_paths}"
