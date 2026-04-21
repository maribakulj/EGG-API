"""Regression tests for Sprint 5 contract + mapper refactor (S5.1 - S5.10)."""

from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.config.models import AppConfig
from app.dependencies import container
from app.schemas.query import NormalizedQuery

# ---------------------------------------------------------------------------
# S5.1 — Literal/Enum in Pydantic models
# ---------------------------------------------------------------------------


def test_s5_1_public_mode_literal_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        AppConfig.model_validate({"auth": {"public_mode": "bogus"}})


def test_s5_1_cors_mode_literal_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        AppConfig.model_validate({"cors": {"mode": "on-maybe"}})


def test_s5_1_criticality_literal_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        AppConfig.model_validate(
            {"mapping": {"id": {"source": "id", "criticality": "super-required"}}}
        )


def test_s5_1_mapping_mode_literal_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        AppConfig.model_validate({"mapping": {"id": {"source": "id", "mode": "teleport"}}})


def test_s5_1_literal_aliases_match_historical_tokens() -> None:
    # The Literal aliases replaced the _VALID_* sets; their values must
    # stay in lockstep with what the admin config API used to accept.
    from app.config.models import CorsMode, Criticality, MappingMode, PublicAuthMode, SameSite

    assert set(PublicAuthMode.__args__) == {  # type: ignore[attr-defined]
        "anonymous_allowed",
        "api_key_optional",
        "api_key_required",
    }
    assert set(CorsMode.__args__) == {"off", "allowlist", "wide_open"}  # type: ignore[attr-defined]
    assert set(SameSite.__args__) == {"strict", "lax", "none"}  # type: ignore[attr-defined]
    assert set(Criticality.__args__) == {  # type: ignore[attr-defined]
        "required",
        "recommended",
        "optional",
    }
    # MappingMode must cover every handler in the dispatch table (see S5.2).
    from app.mappers.schema_mapper import _MODE_HANDLERS

    assert set(MappingMode.__args__) == set(_MODE_HANDLERS.keys())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# S5.2 — SchemaMapper dispatch dict
# ---------------------------------------------------------------------------


def test_s5_2_mapper_dispatch_covers_every_configured_mode() -> None:
    from app.config.models import MappingMode
    from app.mappers.schema_mapper import _MODE_HANDLERS

    declared = set(MappingMode.__args__)  # type: ignore[attr-defined]
    implemented = set(_MODE_HANDLERS.keys())
    assert implemented == declared, (
        f"mode dispatch drift: declared={declared!r} implemented={implemented!r}"
    )


def test_s5_2_mapper_unknown_mode_returns_none() -> None:
    from app.mappers.schema_mapper import SchemaMapper

    # _apply_mode is static and falls back to None for unknown tokens.
    assert SchemaMapper._apply_mode("nonexistent", {}, {}) is None


# ---------------------------------------------------------------------------
# S5.3 — Cache-Control: private when auth required, drop Vary
# ---------------------------------------------------------------------------


def test_s5_3_cache_control_public_for_anonymous_mode(client) -> None:
    container.config_manager.config.auth.public_mode = "anonymous_allowed"
    response = client.get("/v1/search?q=abc")
    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("public, ")
    # Sprint 5: Vary: x-api-key was removed in favor of explicit private caching.
    assert "Vary" not in response.headers


def test_s5_3_cache_control_private_for_api_key_modes(client, admin_headers) -> None:
    container.config_manager.config.auth.public_mode = "api_key_required"
    try:
        response = client.get("/v1/search?q=abc", headers=admin_headers)
        assert response.status_code == 200
        assert response.headers["Cache-Control"].startswith("private, ")
        assert "Vary" not in response.headers
    finally:
        container.config_manager.config.auth.public_mode = "anonymous_allowed"


# ---------------------------------------------------------------------------
# S5.5 — /suggest and /manifest retired from the public contract
# ---------------------------------------------------------------------------


def test_s5_5_manifest_route_absent_from_openapi(client) -> None:
    # /v1/manifest/{id} stays retired (no backend plumbing). /v1/suggest
    # came back in Sprint 8 S8.3 with a real ES-backed implementation.
    schema = client.get("/v1/openapi.json").json()
    paths = schema.get("paths", {})
    assert not any(p.startswith("/v1/manifest") for p in paths)


def test_s5_5_suggest_returns_200_when_backed(client) -> None:
    # Sprint 8 S8.3: /v1/suggest is wired to the adapter's suggest() path;
    # it used to 404 as a 501-stub retirement placeholder.
    response = client.get("/v1/suggest?q=abc")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# S5.6 — Record no longer exposes raw_identifiers
# ---------------------------------------------------------------------------


def test_s5_6_record_schema_no_raw_identifiers(client) -> None:
    schema = client.get("/v1/openapi.json").json()
    record = schema.get("components", {}).get("schemas", {}).get("Record", {})
    props = record.get("properties", {})
    assert "raw_identifiers" not in props
    # Structural / kept fields are still there.
    for field in ("id", "type", "contributors", "media", "identifiers"):
        assert field in props


# ---------------------------------------------------------------------------
# S5.7 — X-Opaque-Id propagation to the backend
# ---------------------------------------------------------------------------


def test_s5_7_x_opaque_id_forwarded_to_backend() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x_opaque_id"] = request.headers.get("x-opaque-id")
        return httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})

    transport = httpx.MockTransport(handler)
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=httpx.Client(transport=transport),
        max_retries=0,
        retry_backoff_seconds=0,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="req-abc-123")
    try:
        adapter.search(NormalizedQuery(q="x"))
    finally:
        structlog.contextvars.clear_contextvars()
    assert seen["x_opaque_id"] == "req-abc-123"


def test_s5_7_no_header_when_context_missing() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x_opaque_id"] = request.headers.get("x-opaque-id")
        return httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})

    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
        retry_backoff_seconds=0,
    )
    structlog.contextvars.clear_contextvars()
    adapter.search(NormalizedQuery(q="x"))
    assert seen.get("x_opaque_id") is None


# ---------------------------------------------------------------------------
# S5.8 — CSV output
# ---------------------------------------------------------------------------


def test_s5_8_csv_format_returns_csv_media_type(client) -> None:
    response = client.get("/v1/search?q=abc&format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers.get("content-disposition", "")


def test_s5_8_csv_header_matches_expected_columns(client) -> None:
    response = client.get("/v1/search?q=abc&format=csv")
    assert response.status_code == 200
    reader = csv.reader(StringIO(response.text))
    header = next(reader)
    assert header == [
        "id",
        "type",
        "title",
        "subtitle",
        "description",
        "creators",
        "languages",
        "subjects",
        "collection",
        "holding_institution",
    ]
    # FakeAdapter returns one hit with creator_csv="A;B" -> list of 2 via
    # the split_list mapper; CSV export joins them with "; ".
    row = next(reader)
    assert row[0] == "1"
    assert row[1] == "object"
    assert row[5] == "A; B"


def test_s5_8_unknown_format_rejected(client) -> None:
    response = client.get("/v1/search?q=abc&format=xml")
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_parameter"
    assert body["error"]["details"]["format"] == "xml"


# ---------------------------------------------------------------------------
# S5.9 — OpenAPI snapshot contract
# ---------------------------------------------------------------------------


# Path snapshot lives in tests/snapshots/openapi_paths.json so an audit
# diff shows a single-file hunk instead of a giant change inside the
# test module. The snapshot is read at call time, not import time, so
# an editor fix-it round-trip (update snapshot + rerun pytest) works
# without touching this file.
_OPENAPI_PATH_SNAPSHOT = Path(__file__).resolve().parent.parent / "snapshots" / "openapi_paths.json"


def test_s5_9_openapi_path_snapshot(client) -> None:
    snapshot = json.loads(_OPENAPI_PATH_SNAPSHOT.read_text())
    expected = set(snapshot["paths"])
    schema = client.get("/v1/openapi.json").json()
    actual = set(schema.get("paths", {}).keys())

    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        msg_lines = [
            "OpenAPI path surface drifted from tests/snapshots/openapi_paths.json.",
            "This is a contract change — decide whether it is major/minor, then:",
            "  1. Update tests/snapshots/openapi_paths.json",
            "  2. Add a CHANGELOG entry under [Unreleased]",
        ]
        if missing:
            msg_lines.append(f"  removed paths: {sorted(missing)}")
        if extra:
            msg_lines.append(f"  added paths:   {sorted(extra)}")
        raise AssertionError("\n".join(msg_lines))


def test_s5_9_every_public_get_has_description(client) -> None:
    schema = client.get("/v1/openapi.json").json()
    paths = schema.get("paths", {})
    for path, operations in paths.items():
        if not path.startswith("/v1/"):
            continue
        get = operations.get("get")
        if get is None:
            continue
        assert get.get("description") or get.get("summary"), (
            f"{path} GET missing description in OpenAPI"
        )


# ---------------------------------------------------------------------------
# S5.10 — CORS allowlist + wide_open coverage
# ---------------------------------------------------------------------------


def _cors_probe_app(mode: str, origins: list[str] | None = None):
    """Build a minimal FastAPI with the ``CORSMiddleware`` under test.

    Pre-Sprint-10 we re-imported ``app.main`` via ``sys.modules.pop`` to
    rebuild its CORS middleware with a new config — that re-ran every
    module-level side effect (container rebuild, tracing bootstrap…)
    and leaked state into whatever test ran next. A local
    ``FastAPI()`` exercises the exact same middleware class with zero
    collateral damage.
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    probe = FastAPI()
    if mode == "allowlist":
        probe.add_middleware(
            CORSMiddleware,
            allow_origins=origins or [],
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["x-api-key", "content-type"],
            max_age=600,
        )
    elif mode == "wide_open":
        probe.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["x-api-key", "content-type"],
            max_age=600,
        )

    @probe.get("/v1/livez")
    def _livez() -> dict[str, str]:
        return {"status": "ok"}

    return probe


def test_s5_10_cors_allowlist_accepts_known_origin() -> None:
    with TestClient(_cors_probe_app("allowlist", ["https://ally.example"])) as tc:
        resp = tc.get("/v1/livez", headers={"Origin": "https://ally.example"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://ally.example"


def test_s5_10_cors_allowlist_denies_unknown_origin() -> None:
    with TestClient(_cors_probe_app("allowlist", ["https://ally.example"])) as tc:
        resp = tc.get("/v1/livez", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200  # CORS doesn't reject; it just omits the header
        assert "access-control-allow-origin" not in resp.headers


def test_s5_10_cors_wide_open_echoes_star() -> None:
    with TestClient(_cors_probe_app("wide_open")) as tc:
        resp = tc.get("/v1/livez", headers={"Origin": "https://anyone.example"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"
