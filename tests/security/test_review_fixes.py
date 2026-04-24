"""Regressions pinning the fixes from the external maturity review.

Each test maps to one claim from the audit:

- R1 cursor pagination bootstraps from the first page
- R2 sort / date_from / date_to / include_fields are actually applied
- R3 /v1/openapi.json strips admin paths; /admin/v1/openapi.json keeps them
- R8 /admin/v1/config masks secrets instead of echoing them

The intent is to *pin the contract* — any future drift in the adapter
translator or redaction flow trips one of these before shipping.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.dependencies import container
from app.schemas.query import NormalizedQuery

# ---------------------------------------------------------------------------
# R8 — Secret redaction
# ---------------------------------------------------------------------------


def test_r8_redact_mask_replaces_secrets_with_sentinel() -> None:
    data = {"auth": {"bootstrap_admin_key": "super-secret"}}
    ConfigManager.redact(data, mask=True)
    assert data["auth"]["bootstrap_admin_key"] == ConfigManager.MASK_SENTINEL


def test_r8_redact_mask_leaves_empty_values_untouched() -> None:
    data = {"auth": {"bootstrap_admin_key": ""}}
    ConfigManager.redact(data, mask=True)
    # Empty → admin can see "not configured", not "***".
    assert data["auth"]["bootstrap_admin_key"] == ""


def test_r8_redact_strip_removes_secret_keys_entirely() -> None:
    data = {"auth": {"bootstrap_admin_key": "super-secret"}}
    ConfigManager.redact(data, mask=False)
    assert "bootstrap_admin_key" not in data["auth"]


def test_r8_redact_walks_nested_paths() -> None:
    data = {"backend": {"auth": {"password": "p", "token": "t"}}}
    ConfigManager.redact(data, mask=True)
    assert data["backend"]["auth"]["password"] == ConfigManager.MASK_SENTINEL
    assert data["backend"]["auth"]["token"] == ConfigManager.MASK_SENTINEL


def test_r8_redact_tolerates_missing_branches() -> None:
    # Should never raise on partial configs; redact is defensive.
    ConfigManager.redact({}, mask=True)
    ConfigManager.redact({"auth": None}, mask=True)
    ConfigManager.redact({"backend": {}}, mask=True)


def test_r8_get_admin_config_masks_bootstrap_admin_key(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    # Seed the in-memory config with a secret, then read back through /config.
    container.config_manager.config.auth.bootstrap_admin_key = "super-secret-admin"
    try:
        response = client.get("/admin/v1/config", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["auth"]["bootstrap_admin_key"] == ConfigManager.MASK_SENTINEL
        # Raw secret must never appear anywhere in the payload.
        assert "super-secret-admin" not in response.text
    finally:
        container.config_manager.config.auth.bootstrap_admin_key = ""


# ---------------------------------------------------------------------------
# R2 — Sort translation (allowed_sorts → ES sort clause)
# ---------------------------------------------------------------------------


@pytest.fixture()
def adapter() -> ElasticsearchAdapter:
    return ElasticsearchAdapter("http://es.local", "records")


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (None, [{"_score": "desc"}, {"_id": "asc"}]),
        ("relevance", [{"_score": "desc"}, {"_id": "asc"}]),
        ("date_desc", [{"date": "desc"}, {"_id": "asc"}]),
        ("date_asc", [{"date": "asc"}, {"_id": "asc"}]),
        ("title_asc", [{"title": "asc"}, {"_id": "asc"}]),
    ],
)
def test_r2_parse_sort_translates_symbolic_names(
    sort: str | None, expected: list[dict[str, str]]
) -> None:
    assert ElasticsearchAdapter._parse_sort(sort) == expected


# ---------------------------------------------------------------------------
# R1 — Cursor pagination bootstraps from the first page
# ---------------------------------------------------------------------------


def test_r1_translate_query_always_emits_sort_without_cursor(
    adapter: ElasticsearchAdapter,
) -> None:
    # Previously the adapter only added sort when ``cursor`` was set, so
    # ``hit.sort`` on the first page was absent and ``next_cursor`` could
    # never be bootstrapped.
    body = adapter.translate_query(NormalizedQuery(q="x", page=1, page_size=10))
    assert body["sort"] == [{"_score": "desc"}, {"_id": "asc"}]
    assert body["from"] == 0
    assert "search_after" not in body


def test_r1_translate_query_sort_survives_cursor_transition(
    adapter: ElasticsearchAdapter,
) -> None:
    # The symbolic sort (``date_desc``) must be preserved when the caller
    # switches to cursor mode — a client paging through a date-sorted feed
    # cannot afford to silently swap sort order mid-scroll.
    from app.adapters.elasticsearch.adapter import _encode_cursor

    cursor = _encode_cursor(["2024-01-01", "doc-1"])
    body = adapter.translate_query(
        NormalizedQuery(q="x", page=1, page_size=10, sort="date_desc", cursor=cursor)
    )
    assert body["sort"] == [{"date": "desc"}, {"_id": "asc"}]
    assert body["search_after"] == ["2024-01-01", "doc-1"]
    assert "from" not in body


# ---------------------------------------------------------------------------
# R2 — Date range filter
# ---------------------------------------------------------------------------


def test_r2_date_from_becomes_range_filter(adapter: ElasticsearchAdapter) -> None:
    body = adapter.translate_query(NormalizedQuery(q="x", date_from="2020-01-01"))
    filters = body["query"]["bool"]["filter"]
    assert {"range": {"date": {"gte": "2020-01-01"}}} in filters


def test_r2_date_to_becomes_range_filter(adapter: ElasticsearchAdapter) -> None:
    body = adapter.translate_query(NormalizedQuery(q="x", date_to="2024-12-31"))
    filters = body["query"]["bool"]["filter"]
    assert {"range": {"date": {"lte": "2024-12-31"}}} in filters


def test_r2_date_from_and_to_combine_in_single_range(
    adapter: ElasticsearchAdapter,
) -> None:
    body = adapter.translate_query(
        NormalizedQuery(q="x", date_from="2020-01-01", date_to="2024-12-31")
    )
    filters = body["query"]["bool"]["filter"]
    assert {"range": {"date": {"gte": "2020-01-01", "lte": "2024-12-31"}}} in filters


def test_r2_no_date_filter_when_bounds_absent(adapter: ElasticsearchAdapter) -> None:
    body = adapter.translate_query(NormalizedQuery(q="x"))
    filters = body["query"]["bool"]["filter"]
    assert not any("range" in f for f in filters)


# ---------------------------------------------------------------------------
# R2 — include_fields (sparse fieldsets) filters the JSON response shape
# ---------------------------------------------------------------------------


def test_r2_include_fields_prunes_record_shape(client: TestClient) -> None:
    # Add "title" to the active include_fields allowlist so the policy
    # accepts it (the default config already allows it).
    response = client.get("/v1/search?q=x&include_fields=title")
    assert response.status_code == 200
    payload = response.json()
    results = payload["results"]
    assert results, "expected at least one result for the contract"
    for record in results:
        # Structural keys always kept.
        assert "id" in record
        assert "type" in record
        # Requested key kept.
        assert "title" in record
        # Fields NOT requested must be absent (principle of least data).
        for absent in ("description", "creators", "links", "availability"):
            assert absent not in record, f"{absent!r} leaked despite include_fields=title"


def test_r2_no_include_fields_returns_full_record_shape(client: TestClient) -> None:
    # The default (unfiltered) path still uses the typed SearchResponse, so
    # every Record field is present (None for unset ones). Regression guard
    # against the sparse-fieldset branch stealing the default flow.
    response = client.get("/v1/search?q=x")
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"], "expected at least one result"
    record = payload["results"][0]
    for key in ("id", "type", "title", "creators", "languages", "links"):
        assert key in record, f"{key!r} missing from default JSON shape"


# ---------------------------------------------------------------------------
# R3 — OpenAPI public/admin split
# ---------------------------------------------------------------------------


def test_r3_public_openapi_drops_admin_tag(client: TestClient) -> None:
    schema = client.get("/v1/openapi.json").json()
    for tag in schema.get("tags", []):
        assert tag.get("name") != "admin", "admin tag leaked into public schema"


def test_r3_admin_openapi_is_auth_gated(client: TestClient) -> None:
    # Anonymous caller: rejected by the router dependency.
    response = client.get("/admin/v1/openapi.json")
    assert response.status_code in {401, 403}


def test_r3_admin_openapi_exposes_full_surface(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    schema = client.get("/admin/v1/openapi.json", headers=admin_headers).json()
    assert "/admin/v1/config" in schema["paths"]
    assert "/v1/search" in schema["paths"]


# ---------------------------------------------------------------------------
# Config save/load round-trip still works under the refactored redact API
# ---------------------------------------------------------------------------


def test_save_still_strips_secrets_from_disk(tmp_path) -> None:
    # Guard against a regression where ``save`` accidentally uses mask=True
    # (leaving ``"***"`` on disk) instead of mask=False (stripping entirely).
    import yaml

    cfg_path = tmp_path / "egg.yaml"
    manager = ConfigManager(path=cfg_path)
    cfg = AppConfig()
    cfg.auth.bootstrap_admin_key = "on-disk-secret"
    manager.save(cfg)

    parsed = yaml.safe_load(cfg_path.read_text())
    assert "bootstrap_admin_key" not in parsed.get("auth", {})
    # Sentinel must not hit disk either.
    assert ConfigManager.MASK_SENTINEL not in cfg_path.read_text()
