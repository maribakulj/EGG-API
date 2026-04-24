"""Versioned SQLite migration runner.

Each migration is an idempotent SQL script that runs at most once. A
``schema_version`` row records the applied version so reruns are no-ops.

Rules:
  - Versions are monotonically increasing integers; never renumber an
    existing migration. Add a new one at the tail instead.
  - Migrations must be idempotent (``CREATE TABLE IF NOT EXISTS``,
    ``ALTER TABLE ... ADD COLUMN`` guarded by PRAGMA checks) so an
    accidental double-apply cannot corrupt state.
  - Data migrations that require context (env vars, config) must NOT
    live here — they belong in a bootstrap step that runs after
    ``migrate()`` returns.
  - Never drop a column in-place: rename the old column via a
    ``CREATE TABLE ... SELECT INTO ... DROP TABLE`` pattern in a
    dedicated migration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    # Accepts a connection so migrations can run multi-statement scripts or
    # PRAGMA-guarded ALTER TABLE safely.
    apply: Callable[[sqlite3.Connection], None]


def _m001_baseline(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT UNIQUE NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            prefix TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS usage_events (
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

        CREATE TABLE IF NOT EXISTS ui_sessions (
            token TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
        CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_usage_events_subject ON usage_events(subject);
        CREATE INDEX IF NOT EXISTS idx_usage_events_status ON usage_events(status_code);
        -- idx_ui_sessions_expires is created by migration 2 once the column
        -- exists; keeping it out of the baseline lets legacy-upgraded dbs
        -- get the ALTER TABLE before the index.
        """
    )


def _m002_ui_sessions_expires_at(conn: sqlite3.Connection) -> None:
    # Idempotent ALTER: add expires_at if missing. Pre-baseline dbs exist
    # in the wild; Sprint 4 consolidates this into a numbered migration.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(ui_sessions)").fetchall()}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE ui_sessions ADD COLUMN expires_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ui_sessions_expires ON ui_sessions(expires_at)")


def _m003_ui_sessions_wipe_pre_hash(conn: sqlite3.Connection) -> None:
    """One-shot wipe of pre-Sprint-1 sessions.

    Sprint 1 S1.6 switched the ``token`` column to store SHA-256(cookie)
    instead of the raw value. Legacy rows carry plaintext tokens that
    the hashed lookup can never match, so this migration discards them
    to force a clean re-authentication. The name is intentionally
    "wipe" not "hash" — nothing is re-hashed here; new rows created
    after this migration ran will already be hashed by the application
    layer. No-op on fresh DBs (DELETE hits 0 rows).
    """
    conn.execute("DELETE FROM ui_sessions")


def _m004_retire_quota_counters(conn: sqlite3.Connection) -> None:
    # Sprint 4 S4.9: quota_counters was created but never wired up. The
    # in-memory InMemoryRateLimiter serves single-worker deployments, and
    # a Redis-backed store is the right answer for multi-worker; this dead
    # table only adds noise on /admin/v1/storage/stats. Drop it and the
    # quota_config companion (no code reads it either — except the one-shot
    # schema marker row, which we migrate out via `schema_version`).
    conn.execute("DROP TABLE IF EXISTS quota_counters")
    conn.execute("DROP TABLE IF EXISTS quota_config")


def _m005_api_keys_hash_variant(conn: sqlite3.Connection) -> None:
    # Sprint 4 S4.6/S4.7: enable HMAC-pepper hashes alongside legacy SHA-256.
    # hash_variant='sha256' (default) covers every existing row; new keys
    # created with a pepper set will store 'hmac_sha256_v1'.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "hash_variant" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN hash_variant TEXT NOT NULL DEFAULT 'sha256'")


def _m006_setup_drafts(conn: sqlite3.Connection) -> None:
    """Sprint 14: per-admin setup wizard draft storage.

    One row per ``key_id`` (the public admin label). The wizard persists
    its state here so an operator can step out of the flow and resume
    later, and so a brief screen refresh does not wipe progress. The
    draft is NOT a config: nothing here reaches ``egg.yaml`` until the
    final step calls ``container.reload()``.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS setup_drafts (
            key_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            step TEXT NOT NULL DEFAULT 'backend',
            updated_at TEXT NOT NULL
        )
        """
    )


def _m007_setup_otps(conn: sqlite3.Connection) -> None:
    """Sprint 16: one-time tokens for the first-run magic link.

    ``egg-api start`` mints an OTP that, when exchanged at
    ``/admin/setup-otp/<token>``, issues a fresh admin UI session
    without forcing the operator to copy-paste the bootstrap key into
    the login form. Tokens are single-use, short-lived (5 minutes by
    default) and hashed at rest — the raw value is only ever shown
    to the terminal that minted it.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS setup_otps (
            token_hash TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_setup_otps_expires ON setup_otps(expires_at)")


def _m008_ui_sessions_last_activity(conn: sqlite3.Connection) -> None:
    """Sprint 18: track per-session last-activity timestamp for idle timeout.

    The TTL column already caps absolute session lifetime. Idle
    timeout is a different policy: kick the session after N minutes
    without a real request. Legacy rows get their ``created_at`` as
    the initial ``last_activity_at`` so the first request after the
    migration does not look anomalously old.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(ui_sessions)").fetchall()}
    if "last_activity_at" not in cols:
        conn.execute("ALTER TABLE ui_sessions ADD COLUMN last_activity_at TEXT")
        conn.execute(
            "UPDATE ui_sessions SET last_activity_at = created_at WHERE last_activity_at IS NULL"
        )


def _m009_import_sources_and_runs(conn: sqlite3.Connection) -> None:
    """Sprint 22: persistent import sources + run history.

    One row per configured upstream (OAI-PMH endpoint, LIDO file
    drop, CSV profile, ...). Each run appends to ``import_runs`` so
    the admin dashboard can show the last ingestion status + a
    history for debugging. ``kind`` is the discriminator so future
    importers (MARC, LIDO, CSV, EAD) can share the same tables.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            kind TEXT NOT NULL,
            url TEXT,
            metadata_prefix TEXT,
            set_spec TEXT,
            schema_profile TEXT NOT NULL DEFAULT 'library',
            created_at TEXT NOT NULL,
            last_run_at TEXT
        );

        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            records_ingested INTEGER NOT NULL DEFAULT 0,
            records_failed INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            FOREIGN KEY(source_id) REFERENCES import_sources(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_import_runs_source ON import_runs(source_id, started_at DESC);
        """
    )


def _m010_import_sources_schedule(conn: sqlite3.Connection) -> None:
    """Sprint 27: cron-like scheduling for import sources.

    ``schedule`` is an enum string (``hourly`` / ``6h`` / ``daily`` /
    ``weekly``) or ``NULL`` for manual-only sources. ``next_run_at`` is
    the absolute ISO timestamp the scheduler uses to pick due sources
    without re-computing cadence on every poll. Using ``ALTER TABLE``
    keeps the column addition idempotent even on databases that were
    upgraded one migration at a time.
    """

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(import_sources)")}
    if "schedule" not in existing:
        conn.execute("ALTER TABLE import_sources ADD COLUMN schedule TEXT")
    if "next_run_at" not in existing:
        conn.execute("ALTER TABLE import_sources ADD COLUMN next_run_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_sources_next_run "
        "ON import_sources(next_run_at) WHERE next_run_at IS NOT NULL"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _m001_baseline),
    Migration(2, "ui_sessions_expires_at", _m002_ui_sessions_expires_at),
    # Version 3 name was "ui_sessions_hash_tokens" pre-Sprint-10 but the
    # function only wiped the table; the new name is honest. The version
    # number and applied-at marker are unchanged, so existing databases
    # still see it as "already applied".
    Migration(3, "ui_sessions_wipe_pre_hash", _m003_ui_sessions_wipe_pre_hash),
    Migration(4, "retire_quota_counters", _m004_retire_quota_counters),
    Migration(5, "api_keys_hash_variant", _m005_api_keys_hash_variant),
    Migration(6, "setup_drafts", _m006_setup_drafts),
    Migration(7, "setup_otps", _m007_setup_otps),
    Migration(8, "ui_sessions_last_activity", _m008_ui_sessions_last_activity),
    Migration(9, "import_sources_and_runs", _m009_import_sources_and_runs),
    Migration(10, "import_sources_schedule", _m010_import_sources_schedule),
)


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    return {row["version"] for row in conn.execute("SELECT version FROM schema_version").fetchall()}


def _baseline_pre_existing_db(conn: sqlite3.Connection, applied: set[int]) -> set[int]:
    """On first upgrade, fast-forward the version pointer for legacy dbs.

    Only baselines *structural* migrations — those that create tables or
    columns idempotently. Data migrations (e.g. the ui_sessions wipe in
    version 3) are left to run, because we cannot safely heuristic-detect
    whether they already happened: a 64-char raw token would look exactly
    like a SHA-256 hash. Re-running migration 3 on an already-hashed DB
    is a no-op (deletes rows that the user will simply re-login to
    recreate), and re-running it on a plaintext DB is the correct action.

    Heuristics (structural only):
      - If `api_keys` already exists, baseline 1 is effectively applied.
      - If `ui_sessions.expires_at` column exists, baseline 2 is applied.
      - If `quota_counters` does not exist, 4 is applied.
      - If `api_keys.hash_variant` column exists, 5 is applied.
      - If `setup_drafts` exists, 6 is applied.
      - If `setup_otps` exists, 7 is applied.
      - If `ui_sessions.last_activity_at` column exists, 8 is applied.
      - If `import_sources` exists, 9 is applied.
    """
    if applied:
        return applied

    def _has_table(name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    baselined: set[int] = set()
    if _has_table("api_keys"):
        baselined.add(1)
    if _has_table("ui_sessions"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ui_sessions)").fetchall()}
        if "expires_at" in cols:
            baselined.add(2)
    if not _has_table("quota_counters") and _has_table("api_keys"):
        baselined.add(4)
    if _has_table("api_keys"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
        if "hash_variant" in cols:
            baselined.add(5)
    if _has_table("setup_drafts"):
        baselined.add(6)
    if _has_table("setup_otps"):
        baselined.add(7)
    if _has_table("ui_sessions"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ui_sessions)").fetchall()}
        if "last_activity_at" in cols:
            baselined.add(8)
    if _has_table("import_sources"):
        baselined.add(9)

    if baselined:
        now = datetime.now(timezone.utc).isoformat()
        for version in sorted(baselined):
            matching = next((m for m in MIGRATIONS if m.version == version), None)
            if matching is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                (version, matching.name, now),
            )
    return baselined


def migrate(conn: sqlite3.Connection) -> list[Migration]:
    """Apply every pending migration. Returns the list that ran."""
    _ensure_schema_version_table(conn)
    applied = _baseline_pre_existing_db(conn, _applied_versions(conn))
    pending = [m for m in MIGRATIONS if m.version not in applied]
    ran: list[Migration] = []
    for migration in pending:
        migration.apply(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, now),
        )
        ran.append(migration)
    conn.commit()
    return ran


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 if the table is absent/empty."""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["v"] or 0) if row else 0
