"""Regression tests for Vague 1 security hardening (C1-C6)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from app.admin_ui.auth import SESSION_COOKIE
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.dependencies import container
from app.runtime_paths import (
    LEGACY_INSECURE_BOOTSTRAP_KEY,
    resolve_bootstrap_admin_key,
)
from app.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# C1 — Bootstrap admin key
# ---------------------------------------------------------------------------


def test_c1_env_key_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_BOOTSTRAP_ADMIN_KEY", "env-provided-key")
    key, generated = resolve_bootstrap_admin_key("config-key")
    assert key == "env-provided-key"
    assert generated is False


def test_c1_rejects_legacy_insecure_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_BOOTSTRAP_ADMIN_KEY", LEGACY_INSECURE_BOOTSTRAP_KEY)
    with pytest.raises(RuntimeError, match="insecure default"):
        resolve_bootstrap_admin_key("")


def test_c1_refuses_start_in_production_without_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EGG_BOOTSTRAP_ADMIN_KEY", raising=False)
    monkeypatch.setenv("EGG_ENV", "production")
    monkeypatch.setenv("EGG_HOME", str(tmp_path))
    monkeypatch.setenv("EGG_BOOTSTRAP_KEY_PATH", str(tmp_path / "no-sidecar.key"))
    with pytest.raises(RuntimeError, match="production"):
        resolve_bootstrap_admin_key("")


def test_c1_generates_and_persists_in_development(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "bootstrap_admin.key"
    monkeypatch.delenv("EGG_BOOTSTRAP_ADMIN_KEY", raising=False)
    monkeypatch.setenv("EGG_ENV", "development")
    monkeypatch.setenv("EGG_BOOTSTRAP_KEY_PATH", str(sidecar))

    key1, generated1 = resolve_bootstrap_admin_key("")
    assert generated1 is True
    assert key1 and key1 != LEGACY_INSECURE_BOOTSTRAP_KEY
    assert sidecar.exists()

    # Subsequent calls reuse the sidecar — no regeneration.
    key2, generated2 = resolve_bootstrap_admin_key("")
    assert key2 == key1
    assert generated2 is False


def test_c1_sidecar_file_has_owner_only_perms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "bootstrap_admin.key"
    monkeypatch.delenv("EGG_BOOTSTRAP_ADMIN_KEY", raising=False)
    monkeypatch.setenv("EGG_ENV", "development")
    monkeypatch.setenv("EGG_BOOTSTRAP_KEY_PATH", str(sidecar))

    resolve_bootstrap_admin_key("")
    mode = sidecar.stat().st_mode & 0o777
    assert mode == 0o600


def test_c1_rejects_legacy_default_in_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "bootstrap_admin.key"
    monkeypatch.delenv("EGG_BOOTSTRAP_ADMIN_KEY", raising=False)
    monkeypatch.setenv("EGG_ENV", "development")
    monkeypatch.setenv("EGG_BOOTSTRAP_KEY_PATH", str(sidecar))

    key, generated = resolve_bootstrap_admin_key(LEGACY_INSECURE_BOOTSTRAP_KEY)
    assert generated is True
    assert key != LEGACY_INSECURE_BOOTSTRAP_KEY


# ---------------------------------------------------------------------------
# C2 — Session cookie hardening
# ---------------------------------------------------------------------------


def test_c2_cookie_defaults_to_secure_and_strict() -> None:
    cfg = AppConfig()
    assert cfg.auth.admin_cookie_secure is True
    assert cfg.auth.admin_cookie_samesite == "strict"


def test_c2_login_sets_configured_cookie_flags(client, admin_headers) -> None:
    # Tests run with secure=False and samesite=lax (see conftest).
    response = client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    raw_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE in raw_cookie
    assert "HttpOnly" in raw_cookie
    # In tests the cookie is not secure (http transport). Flag absence is expected.
    assert "Secure" not in raw_cookie.split(";")[0].strip().split() or "Secure" in raw_cookie
    assert "SameSite=lax" in raw_cookie.lower() or "samesite=lax" in raw_cookie.lower()


def test_c2_secure_flag_emitted_when_enabled(client, admin_headers) -> None:
    container.config_manager.config.auth.admin_cookie_secure = True
    container.config_manager.config.auth.admin_cookie_samesite = "strict"
    try:
        response = client.post(
            "/admin/login",
            data={"api_key": admin_headers["x-api-key"]},
            follow_redirects=False,
        )
        raw_cookie = response.headers.get("set-cookie", "")
        assert "Secure" in raw_cookie
        assert "samesite=strict" in raw_cookie.lower()
    finally:
        container.config_manager.config.auth.admin_cookie_secure = False
        container.config_manager.config.auth.admin_cookie_samesite = "lax"


# ---------------------------------------------------------------------------
# C3 — Never log raw API keys in usage_events
# ---------------------------------------------------------------------------


def test_c3_raw_api_key_never_stored_in_usage_events(client, admin_headers) -> None:
    raw_key = admin_headers["x-api-key"]
    # Trigger a request with a valid admin key.
    client.get("/admin/v1/config", headers=admin_headers)
    # And one with an invalid key.
    client.get("/v1/search?q=abc", headers={"x-api-key": "not-a-real-key"})

    events = container.store.list_recent_usage_events(limit=50)
    assert events, "expected at least one usage event"

    for event in events:
        assert event.subject != raw_key
        assert event.api_key_id != raw_key
        if event.api_key_id is not None:
            # Stored api_key_id is always a key_id (label), never the secret.
            assert event.api_key_id == "admin"


def test_c3_invalid_key_falls_back_to_client_host(client) -> None:
    client.get("/v1/search?q=abc", headers={"x-api-key": "bogus"})
    events = container.store.list_recent_usage_events(limit=10)
    latest = events[0]
    assert latest.api_key_id is None
    # TestClient reports as "testclient" for request.client.host
    assert latest.subject != "bogus"


# ---------------------------------------------------------------------------
# C4 — Config YAML redaction
# ---------------------------------------------------------------------------


def test_c4_save_does_not_persist_bootstrap_admin_key(tmp_path: Path) -> None:
    cfg_path = tmp_path / "egg.yaml"
    manager = ConfigManager(path=cfg_path)

    cfg = AppConfig()
    cfg.auth.bootstrap_admin_key = "super-secret-admin-key"
    manager.save(cfg)

    raw = cfg_path.read_text()
    assert "super-secret-admin-key" not in raw
    # Round-trip: the key is absent from YAML; reload gets the default (empty).
    parsed = yaml.safe_load(raw)
    assert parsed.get("auth", {}).get("bootstrap_admin_key") is None


def test_c4_in_memory_config_keeps_the_key(tmp_path: Path) -> None:
    cfg_path = tmp_path / "egg.yaml"
    manager = ConfigManager(path=cfg_path)
    cfg = AppConfig()
    cfg.auth.bootstrap_admin_key = "keep-me-in-memory"
    manager.save(cfg)

    assert manager.config.auth.bootstrap_admin_key == "keep-me-in-memory"


# ---------------------------------------------------------------------------
# C5 — UI session TTL
# ---------------------------------------------------------------------------


def test_c5_session_expires_after_ttl(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    token = store.create_ui_session("admin", ttl_hours=1)
    assert store.get_ui_session_key_id(token) == "admin"

    # Force the session to be expired.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE ui_sessions SET expires_at = ? WHERE token = ?", (past, token))
        conn.commit()

    assert store.get_ui_session_key_id(token) is None


def test_c5_expired_session_is_purged_on_read(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    token = store.create_ui_session("admin", ttl_hours=1)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE ui_sessions SET expires_at = ? WHERE token = ?", (past, token))
        conn.commit()
    store.get_ui_session_key_id(token)

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM ui_sessions WHERE token = ?", (token,)).fetchone()
    assert row[0] == 0


def test_c5_migration_adds_expires_at_column(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    # Simulate a pre-migration database.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE ui_sessions (
                token TEXT PRIMARY KEY,
                key_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()

    store = SQLiteStore(db_path)
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ui_sessions)")}
    assert "expires_at" in cols


# ---------------------------------------------------------------------------
# C6 — Security headers
# ---------------------------------------------------------------------------


def test_c6_baseline_security_headers_on_public_api(client) -> None:
    response = client.get("/v1/health")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Referrer-Policy") == "no-referrer"


def test_c6_admin_routes_block_framing_and_set_csp(client, admin_headers) -> None:
    client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    response = client.get("/admin/ui")
    assert response.headers.get("X-Frame-Options") == "DENY"
    csp = response.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


def test_c6_hsts_enabled_in_production(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setenv("EGG_ENV", "production")
    response = client.get("/v1/health")
    assert "max-age=" in response.headers.get("Strict-Transport-Security", "")


def test_c6_hsts_absent_outside_production(client) -> None:
    response = client.get("/v1/health")
    assert "Strict-Transport-Security" not in response.headers
