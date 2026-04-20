"""Regression tests for Sprint 4 persistence hardening (S4.1 - S4.9)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.migrations import MIGRATIONS, current_version, migrate
from app.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# S4.1 — versioned migration runner
# ---------------------------------------------------------------------------


def test_s4_1_fresh_db_reaches_latest_version(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    assert store.schema_version() == MIGRATIONS[-1].version


def test_s4_1_migrate_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    first = store.schema_version()
    store.initialize()  # second call must not re-run migrations
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == [m.version for m in MIGRATIONS]
    assert store.schema_version() == first


def test_s4_1_records_every_migration_with_name(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT version, name FROM schema_version ORDER BY version").fetchall()
    assert [(r["version"], r["name"]) for r in rows] == [(m.version, m.name) for m in MIGRATIONS]


# ---------------------------------------------------------------------------
# S4.2 — legacy db baseline
# ---------------------------------------------------------------------------


def test_s4_2_legacy_pre_migration_db_is_baselined_without_data_loss(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    # Shape that matches Sprint 0-era initialize() output: api_keys + ui_sessions
    # with expires_at already added, no schema_version row.
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT UNIQUE NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                prefix TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_used_at TEXT
            );
            CREATE TABLE ui_sessions (
                token TEXT PRIMARY KEY,
                key_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            );
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                method TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                api_key_id TEXT,
                subject TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                error_code TEXT
            );
            INSERT INTO api_keys(key_id, key_hash, prefix, status, created_at)
            VALUES ('legacy', 'aaaabbbb', 'aaaabbbb', 'active', '2026-01-01T00:00:00Z');
            INSERT INTO usage_events(request_id, timestamp, endpoint, method, status_code, subject, latency_ms)
            VALUES ('r1', '2026-01-01T00:00:00Z', '/v1/search', 'GET', 200, 'ip:127.0.0.1', 1);
            """
        )
        conn.commit()

    store = SQLiteStore(db)
    store.initialize()

    # Legacy rows survived and the pointer advanced to the latest version.
    assert store.schema_version() == MIGRATIONS[-1].version
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        keys = conn.execute("SELECT key_id FROM api_keys").fetchall()
        events = conn.execute("SELECT request_id FROM usage_events").fetchall()
    assert [r["key_id"] for r in keys] == ["legacy"]
    assert [r["request_id"] for r in events] == ["r1"]


def test_s4_2_fresh_db_records_baseline_migration(tmp_path: Path) -> None:
    # Migration 1 must always land in schema_version on a brand-new database,
    # even though there are no pre-existing tables to baseline.
    store = SQLiteStore(tmp_path / "fresh.sqlite3")
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()}
    assert 1 in versions


# ---------------------------------------------------------------------------
# S4.9 — quota_counters and quota_config retired
# ---------------------------------------------------------------------------


def test_s4_9_quota_tables_removed_after_migrate(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "q.sqlite3")
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "quota_counters" not in tables
    assert "quota_config" not in tables


# ---------------------------------------------------------------------------
# S4.6 + S4.7 — HMAC+pepper + legacy compatibility
# ---------------------------------------------------------------------------


def test_s4_6_pepper_produces_hmac_hash(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "pepper.sqlite3", pepper=b"super-secret-pepper")
    store.initialize()
    secret, _ = store.create_api_key("pepper-key")
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT key_hash, hash_variant FROM api_keys WHERE key_id = ?",
            ("pepper-key",),
        ).fetchone()
    # HMAC != plain SHA-256 for the same input.
    import hashlib

    assert row["key_hash"] != hashlib.sha256(secret.encode()).hexdigest()
    assert row["hash_variant"] == "hmac_sha256_v1"


def test_s4_6_legacy_sha256_keys_still_validate_when_pepper_enabled(tmp_path: Path) -> None:
    # Step 1: create a store without a pepper -> legacy SHA-256 row.
    store_no_pepper = SQLiteStore(tmp_path / "mixed.sqlite3")
    store_no_pepper.initialize()
    legacy_secret, _ = store_no_pepper.create_api_key("legacy-key")

    # Step 2: a fresh store instance enables the pepper.
    store_with_pepper = SQLiteStore(tmp_path / "mixed.sqlite3", pepper=b"new-pepper")
    assert store_with_pepper.validate_api_key(legacy_secret) is not None


def test_s4_6_pepper_lookup_wins_when_both_variants_configured(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "both.sqlite3", pepper=b"pepper-1")
    store.initialize()
    secret, _ = store.create_api_key("both-variants")
    assert store.validate_api_key(secret) is not None


def test_s4_6_no_pepper_falls_back_to_sha256(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_API_KEY_PEPPER", raising=False)
    store = SQLiteStore(tmp_path / "plain.sqlite3")
    store.initialize()
    secret, _ = store.create_api_key("plain-key")
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT hash_variant FROM api_keys WHERE key_id = ?", ("plain-key",)
        ).fetchone()
    assert row["hash_variant"] == "sha256"
    assert store.validate_api_key(secret) is not None


def test_s4_7_rotate_upgrades_legacy_row_to_pepper(tmp_path: Path) -> None:
    store_legacy = SQLiteStore(tmp_path / "rot.sqlite3")
    store_legacy.initialize()
    legacy_secret, _ = store_legacy.create_api_key("rotatable")
    assert store_legacy.validate_api_key(legacy_secret) is not None

    store_with_pepper = SQLiteStore(tmp_path / "rot.sqlite3", pepper=b"rotation-pepper")
    new_secret = store_with_pepper.rotate_api_key("rotatable")
    assert new_secret and new_secret != legacy_secret
    # Old secret is gone after rotation (rotate overwrites the hash).
    assert store_with_pepper.validate_api_key(legacy_secret) is None
    assert store_with_pepper.validate_api_key(new_secret) is not None

    with sqlite3.connect(store_with_pepper.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT hash_variant FROM api_keys WHERE key_id = ?", ("rotatable",)
        ).fetchone()
    assert row["hash_variant"] == "hmac_sha256_v1"


# ---------------------------------------------------------------------------
# S4.3 + S4.4 — retention purges
# ---------------------------------------------------------------------------


def test_s4_3_purge_expired_ui_sessions(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "purge.sqlite3")
    store.initialize()
    store.create_ui_session("admin", ttl_hours=1)
    # Rewind the row's expires_at by hand.
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE ui_sessions SET expires_at = ?", (past,))
        conn.commit()

    deleted = store.purge_expired_ui_sessions()
    assert deleted == 1
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM ui_sessions").fetchone()
    assert rows[0] == 0


def test_s4_4_purge_usage_events_respects_retention(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "retention.sqlite3")
    store.initialize()

    # One old event, one fresh one.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO usage_events(request_id, timestamp, endpoint, method, status_code, "
            "api_key_id, subject, latency_ms, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old", old_ts, "/v1/search", "GET", 200, None, "ip:1.2.3.4", 1, None),
        )
        conn.commit()
    store.log_usage_event(
        request_id="fresh",
        endpoint="/v1/search",
        method="GET",
        status_code=200,
        api_key_id=None,
        subject="ip:1.2.3.4",
        latency_ms=1,
        error_code=None,
    )

    removed = store.purge_usage_events_older_than(30)
    assert removed == 1
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        remaining = [r["request_id"] for r in conn.execute("SELECT request_id FROM usage_events")]
    assert remaining == ["fresh"]


def test_s4_4_zero_retention_is_a_no_op(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "zero.sqlite3")
    store.initialize()
    store.log_usage_event(
        request_id="r",
        endpoint="/v1/livez",
        method="GET",
        status_code=200,
        api_key_id=None,
        subject="ip:127.0.0.1",
        latency_ms=1,
        error_code=None,
    )
    assert store.purge_usage_events_older_than(0) == 0
    assert store.count_usage_events() == 1


# ---------------------------------------------------------------------------
# S4.5 — storage_stats endpoint
# ---------------------------------------------------------------------------


def test_s4_5_storage_stats_shape(client, admin_headers) -> None:
    # Generate activity so the counts are non-zero.
    client.get("/v1/livez")
    response = client.get("/admin/v1/storage/stats", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()

    for key in (
        "rows_api_keys",
        "rows_ui_sessions",
        "rows_usage_events",
        "rows_schema_version",
        "schema_version",
        "db_path",
        "db_size_bytes",
        "last_purge",
        "retention_days",
        "purge_interval_seconds",
    ):
        assert key in body, f"missing key {key!r}"
    assert body["schema_version"] == MIGRATIONS[-1].version
    assert body["rows_api_keys"] >= 1  # at least the admin key
    assert body["rows_usage_events"] >= 1  # we just hit /v1/livez


def test_s4_5_storage_stats_requires_admin(client) -> None:
    response = client.get("/admin/v1/storage/stats")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# S4.1 — `egg-api migrate` CLI
# ---------------------------------------------------------------------------


def test_s4_1_cli_migrate_reports_applied(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    from app import cli

    # NOTE: conftest.reset_container initializes ``tmp_path / 'state.sqlite3'``
    # already; using the same name would leak that migration into the CLI
    # test's "before" reading. A unique filename keeps the DB pristine.
    config = tmp_path / "config" / "egg.yaml"
    db = tmp_path / "cli-migrate.sqlite3"
    monkeypatch.setenv("EGG_CONFIG_PATH", str(config))
    monkeypatch.setenv("EGG_STATE_DB_PATH", str(db))
    monkeypatch.setenv("EGG_BOOTSTRAP_ADMIN_KEY", "cli-test-admin-key")

    parser = cli.build_parser()
    # First call: db does not exist yet, migrate creates and stamps every
    # version.
    args = parser.parse_args(["migrate"])
    rc = args.func(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["after"] == MIGRATIONS[-1].version
    assert out["before"] == 0
    assert len(out["applied"]) == len(MIGRATIONS)

    # Second call: nothing to do -> empty applied list, version unchanged.
    args = parser.parse_args(["migrate"])
    rc = args.func(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] == []
    assert out["before"] == out["after"] == MIGRATIONS[-1].version


# ---------------------------------------------------------------------------
# Migration runner unit coverage
# ---------------------------------------------------------------------------


def test_migrate_returns_the_migrations_it_ran(tmp_path: Path) -> None:
    db = tmp_path / "m.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        ran = migrate(conn)
        assert [m.version for m in ran] == [m.version for m in MIGRATIONS]
        assert current_version(conn) == MIGRATIONS[-1].version
