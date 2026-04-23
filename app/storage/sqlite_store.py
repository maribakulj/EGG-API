from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.migrations import current_version as _schema_version, migrate

# Hash variant tags stored in api_keys.hash_variant. Legacy rows remain on
# plain SHA-256; new rows use HMAC-SHA256 keyed by the configured pepper.
_VARIANT_SHA256 = "sha256"
_VARIANT_HMAC_V1 = "hmac_sha256_v1"


@dataclass
class ApiKeyRecord:
    key_id: str
    status: str
    created_at: str
    prefix: str
    last_used_at: str | None = None


@dataclass
class UsageEvent:
    timestamp: str
    endpoint: str
    method: str
    status_code: int
    api_key_id: str | None
    subject: str
    latency_ms: int
    error_code: str | None


@dataclass
class ImportSource:
    id: int
    label: str
    kind: str  # "oaipmh" / "oaipmh_lido" / "oaipmh_marcxml" / "oaipmh_ead" /
    # "lido_file" / "marc_file" / "marcxml_file" / "csv_file" / "ead_file"
    url: str | None
    metadata_prefix: str | None
    set_spec: str | None
    schema_profile: str  # "library" / "museum" / "archive" / "custom"
    created_at: str
    last_run_at: str | None = None
    # Sprint 27: cron-like schedule. ``None`` / empty → manual runs only.
    # Values: "hourly" / "6h" / "daily" / "weekly" (keep the list small
    # so the UI stays a dropdown non-technical operators can use).
    schedule: str | None = None
    next_run_at: str | None = None


@dataclass
class ImportRun:
    id: int
    source_id: int
    started_at: str
    ended_at: str | None
    status: str  # "running" / "succeeded" / "failed"
    records_ingested: int
    records_failed: int
    error_message: str | None


class SQLiteStore:
    """Thread-local SQLite store.

    Each OS thread keeps its own persistent ``sqlite3.Connection`` keyed by
    ``db_path``; FastAPI's sync routes (which run in a threadpool) reuse the
    same connection across calls. WAL journaling lets multiple threads
    read/write concurrently. Connections are opened with
    ``check_same_thread=False`` on the instance but the thread-local pool
    guarantees no connection is used from two threads at once.

    ``pepper`` (optional): when non-empty, new API keys are stored as
    HMAC-SHA256(pepper, secret). If unset, keys fall back to plain SHA-256
    so existing deployments keep working. Validation always tries both
    variants — legacy rows validate via SHA-256 even after a pepper is
    introduced, until the operator rotates them.
    """

    def __init__(self, db_path: Path, *, pepper: bytes | None = None) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        if pepper is None:
            env = os.getenv("EGG_API_KEY_PEPPER", "").strip()
            pepper = env.encode() if env else None
        self._pepper: bytes | None = pepper or None

    def _connect(self) -> sqlite3.Connection:
        # Reuse the thread's connection when it points at the same DB file;
        # re-open it if the store was pointed at a different path (typical in
        # tests that swap the underlying file between fixtures).
        cached = getattr(self._local, "conn", None)
        cached_path = getattr(self._local, "db_path", None)
        if cached is not None and cached_path == self.db_path:
            return cached
        if cached is not None:
            with contextlib.suppress(sqlite3.Error):
                cached.close()
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._local.conn = conn
        self._local.db_path = self.db_path
        return conn

    def close(self) -> None:
        """Close the current thread's cached connection, if any."""
        cached = getattr(self._local, "conn", None)
        if cached is not None:
            try:
                cached.close()
            finally:
                self._local.conn = None
                self._local.db_path = None

    def initialize(self) -> None:
        """Create or upgrade the schema via the versioned migration runner."""
        with self._connect() as conn:
            migrate(conn)

    def schema_version(self) -> int:
        with self._connect() as conn:
            return _schema_version(conn)

    @staticmethod
    def _hash_key(key: str) -> str:
        """Legacy SHA-256 of the raw secret (kept for migration/backwards compat)."""
        return hashlib.sha256(key.encode()).hexdigest()

    def _hash_with_variant(self, key: str) -> tuple[str, str]:
        """Return ``(digest, variant)`` for the current store configuration.

        Falls back to plain SHA-256 when no pepper is configured so deployments
        without the env var keep working.
        """
        if self._pepper:
            digest = hmac.new(self._pepper, key.encode(), hashlib.sha256).hexdigest()
            return digest, _VARIANT_HMAC_V1
        return self._hash_key(key), _VARIANT_SHA256

    def _candidate_hashes(self, key: str) -> list[tuple[str, str]]:
        """Return every hash shape to try when looking a key up.

        Validation checks the pepper variant first (if configured) then the
        legacy SHA-256 so freshly created keys win the race and legacy keys
        still validate until they are rotated.
        """
        candidates: list[tuple[str, str]] = []
        if self._pepper:
            candidates.append(
                (
                    hmac.new(self._pepper, key.encode(), hashlib.sha256).hexdigest(),
                    _VARIANT_HMAC_V1,
                )
            )
        candidates.append((self._hash_key(key), _VARIANT_SHA256))
        return candidates

    def ensure_admin_key(self, key: str, key_id: str = "admin") -> None:
        now = datetime.now(timezone.utc).isoformat()
        digest, variant = self._hash_with_variant(key)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO api_keys(key_id, key_hash, prefix, status, created_at, hash_variant)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (key_id, digest, key[:8], now, variant),
            )

    def create_api_key(self, key_id: str) -> tuple[str, ApiKeyRecord]:
        secret = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc).isoformat()
        digest, variant = self._hash_with_variant(secret)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_keys(key_id, key_hash, prefix, status, created_at, hash_variant)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (key_id, digest, secret[:8], now, variant),
            )
        return secret, ApiKeyRecord(
            key_id=key_id, status="active", created_at=now, prefix=secret[:8]
        )

    def list_api_keys(self) -> list[ApiKeyRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_id, status, created_at, prefix, last_used_at FROM api_keys ORDER BY key_id"
            ).fetchall()
        return [ApiKeyRecord(**dict(row)) for row in rows]

    def set_key_status_by_key_id(self, key_id: str, status: str) -> bool:
        """Flip the status row identified by its public label."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET status = ? WHERE key_id = ?",
                (status, key_id),
            )
        return cur.rowcount > 0

    def set_key_status_by_secret(self, secret: str, status: str) -> bool:
        """Flip the status row identified by its raw key value (hashed for lookup).

        Tries every known hash variant so legacy SHA-256 rows remain
        reachable by secret even after the pepper is enabled.
        """
        hashes = [digest for digest, _ in self._candidate_hashes(secret)]
        placeholders = ",".join("?" for _ in hashes)
        # `placeholders` is a fixed string of question marks; never a user
        # value. Parameterization on `hashes` is preserved.
        sql = f"UPDATE api_keys SET status = ? WHERE key_hash IN ({placeholders})"  # noqa: S608
        with self._connect() as conn:
            cur = conn.execute(sql, (status, *hashes))
        return cur.rowcount > 0

    def rotate_api_key(self, key_id: str) -> str | None:
        """Generate a fresh secret for ``key_id`` and replace the stored hash.

        Returns the new raw secret (only time it is ever disclosed), or None
        when the key_id is unknown. The new hash always uses the store's
        current variant — rotation is how an operator migrates a legacy
        SHA-256 row onto the pepper.
        """
        new_secret = secrets.token_urlsafe(24)
        digest, variant = self._hash_with_variant(new_secret)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET key_hash = ?, prefix = ?, status = 'active', "
                "hash_variant = ? WHERE key_id = ?",
                (digest, new_secret[:8], variant, key_id),
            )
            if cur.rowcount == 0:
                return None
        return new_secret

    def set_key_status(self, secret_or_key_id: str, status: str) -> bool:
        """Legacy shim: try key_id first, fall back to secret.

        Prefer :meth:`set_key_status_by_key_id` or :meth:`set_key_status_by_secret`
        in new code to avoid SQL clauses that match on either column at once.
        """
        if self.set_key_status_by_key_id(secret_or_key_id, status):
            return True
        return self.set_key_status_by_secret(secret_or_key_id, status)

    def validate_api_key(self, secret: str | None) -> ApiKeyRecord | None:
        """Look the secret up under every supported hash variant.

        When a pepper is configured the HMAC variant is checked first, so
        freshly minted keys win the hot path; legacy SHA-256 rows still
        validate until the operator rotates them.
        """
        if not secret:
            return None
        hashes = [digest for digest, _ in self._candidate_hashes(secret)]
        placeholders = ",".join("?" for _ in hashes)
        # placeholders is a fixed count of literal question marks.
        sql = f"SELECT key_id, status, created_at, prefix, last_used_at FROM api_keys WHERE key_hash IN ({placeholders})"  # noqa: S608
        with self._connect() as conn:
            row = conn.execute(sql, tuple(hashes)).fetchone()
            if not row:
                return None
            record = ApiKeyRecord(**dict(row))
            if record.status != "active":
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                (datetime.now(timezone.utc).isoformat(), record.key_id),
            )
            return record

    @staticmethod
    def _hash_session_token(token: str) -> str:
        # Same primitive as _hash_key; DB rows never store the raw cookie.
        return hashlib.sha256(token.encode()).hexdigest()

    def create_ui_session(self, key_id: str, ttl_hours: int = 12) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_session_token(token)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=max(1, int(ttl_hours)))).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ui_sessions(token, key_id, created_at, expires_at, "
                "last_activity_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash, key_id, now.isoformat(), expires_at, now.isoformat()),
            )
        return token

    def get_ui_session_key_id(
        self, token: str | None, *, idle_timeout_minutes: int = 0
    ) -> str | None:
        """Return the ``key_id`` owning ``token``, or ``None``.

        ``idle_timeout_minutes`` (0 = disabled) kicks sessions that
        have not seen any activity for N minutes. Every successful
        lookup bumps ``last_activity_at``, which also serves as a
        telemetry anchor for ``/admin/v1/status``.
        """
        if not token:
            return None
        token_hash = self._hash_session_token(token)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key_id, expires_at, last_activity_at FROM ui_sessions WHERE token = ?",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            expires_at = row["expires_at"]
            if expires_at is not None and expires_at <= now_iso:
                conn.execute("DELETE FROM ui_sessions WHERE token = ?", (token_hash,))
                return None
            # Idle timeout: kick if the last activity predates the cutoff.
            if idle_timeout_minutes > 0:
                last = row["last_activity_at"] or row["expires_at"] or now_iso
                cutoff = (now - timedelta(minutes=int(idle_timeout_minutes))).isoformat()
                if last <= cutoff:
                    conn.execute("DELETE FROM ui_sessions WHERE token = ?", (token_hash,))
                    return None
            # Touch the activity timestamp so subsequent reads shift
            # the idle window forward.
            conn.execute(
                "UPDATE ui_sessions SET last_activity_at = ? WHERE token = ?",
                (now_iso, token_hash),
            )
        return str(row["key_id"])

    def invalidate_sessions_for_key_id(self, key_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM ui_sessions WHERE key_id = ?", (key_id,))
        return int(cur.rowcount or 0)

    def delete_ui_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = self._hash_session_token(token)
        with self._connect() as conn:
            conn.execute("DELETE FROM ui_sessions WHERE token = ?", (token_hash,))

    def purge_expired_ui_sessions(self) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM ui_sessions WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now_iso,),
            )
        return int(cur.rowcount or 0)

    # -- Setup wizard drafts (Sprint 14) -------------------------------------
    def load_setup_draft(self, key_id: str) -> tuple[dict[str, object], str] | None:
        """Return ``(payload, step)`` for ``key_id`` or ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, step FROM setup_drafts WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"])
        except (ValueError, TypeError):
            # Corrupt row: treat as absent; the caller will rebuild.
            return None
        if not isinstance(data, dict):
            return None
        return data, str(row["step"])

    def save_setup_draft(self, key_id: str, payload: dict[str, object], step: str) -> None:
        """Upsert a wizard draft for ``key_id``."""
        now_iso = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(payload, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO setup_drafts(key_id, payload, step, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key_id) DO UPDATE SET
                    payload = excluded.payload,
                    step = excluded.step,
                    updated_at = excluded.updated_at
                """,
                (key_id, serialized, step, now_iso),
            )

    def delete_setup_draft(self, key_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM setup_drafts WHERE key_id = ?", (key_id,))

    # -- First-run OTP tokens (Sprint 16) ------------------------------------
    def create_setup_otp(self, key_id: str, *, ttl_seconds: int = 300) -> str:
        """Mint a one-time token redeemable at ``/admin/setup-otp/{token}``.

        Returns the raw token (shown once to the CLI user). Only its
        SHA-256 hash lands in the DB, so an attacker with read access
        to the state file cannot replay it.
        """
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(30, int(ttl_seconds)))).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO setup_otps(token_hash, key_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (digest, key_id, now.isoformat(), expires),
            )
        return token

    def consume_setup_otp(self, token: str | None) -> str | None:
        """Spend the OTP and return the owner ``key_id``, or ``None``.

        The token is matched by hash, must not be expired, and must not
        have been consumed already. Consumption is recorded inline so a
        replay attempt gets ``None`` rather than a second admin session.
        """
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT key_id, expires_at, consumed_at
                FROM setup_otps WHERE token_hash = ?
                """,
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if row["consumed_at"] is not None:
                return None
            if row["expires_at"] <= now_iso:
                return None
            conn.execute(
                "UPDATE setup_otps SET consumed_at = ? WHERE token_hash = ?",
                (now_iso, digest),
            )
        return str(row["key_id"])

    def purge_expired_setup_otps(self) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM setup_otps WHERE expires_at <= ? OR consumed_at IS NOT NULL",
                (now_iso,),
            )
        return int(cur.rowcount or 0)

    # -- Import sources + runs (Sprint 22 + 27) ------------------------------
    def add_import_source(
        self,
        *,
        label: str,
        kind: str,
        url: str | None = None,
        metadata_prefix: str | None = None,
        set_spec: str | None = None,
        schema_profile: str = "library",
        schedule: str | None = None,
        next_run_at: str | None = None,
    ) -> ImportSource:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO import_sources(
                    label, kind, url, metadata_prefix, set_spec,
                    schema_profile, created_at, schedule, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    label,
                    kind,
                    url,
                    metadata_prefix,
                    set_spec,
                    schema_profile,
                    now,
                    schedule,
                    next_run_at,
                ),
            )
            row_id = int(cur.lastrowid or 0)
        return ImportSource(
            id=row_id,
            label=label,
            kind=kind,
            url=url,
            metadata_prefix=metadata_prefix,
            set_spec=set_spec,
            schema_profile=schema_profile,
            created_at=now,
            last_run_at=None,
            schedule=schedule,
            next_run_at=next_run_at,
        )

    def list_import_sources(self) -> list[ImportSource]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, label, kind, url, metadata_prefix, set_spec,
                       schema_profile, created_at, last_run_at,
                       schedule, next_run_at
                FROM import_sources
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [ImportSource(**dict(row)) for row in rows]

    def get_import_source(self, source_id: int) -> ImportSource | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, label, kind, url, metadata_prefix, set_spec,
                       schema_profile, created_at, last_run_at,
                       schedule, next_run_at
                FROM import_sources WHERE id = ?
                """,
                (int(source_id),),
            ).fetchone()
        return ImportSource(**dict(row)) if row else None

    def list_due_import_sources(self, *, now: str) -> list[ImportSource]:
        """Return sources whose ``next_run_at`` is non-null and ``<= now``."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, label, kind, url, metadata_prefix, set_spec,
                       schema_profile, created_at, last_run_at,
                       schedule, next_run_at
                FROM import_sources
                WHERE next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now,),
            ).fetchall()
        return [ImportSource(**dict(row)) for row in rows]

    def set_import_source_schedule(
        self,
        source_id: int,
        *,
        schedule: str | None,
        next_run_at: str | None,
    ) -> bool:
        """Write back a new schedule + next_run_at. Returns ``True`` if the
        row existed."""

        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE import_sources SET schedule = ?, next_run_at = ? WHERE id = ?",
                (schedule, next_run_at, int(source_id)),
            )
        return int(cur.rowcount or 0) > 0

    def delete_import_source(self, source_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM import_sources WHERE id = ?", (int(source_id),))
        return int(cur.rowcount or 0) > 0

    def start_import_run(self, source_id: int) -> int:
        """Record the start of an import; returns the new run id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO import_runs(source_id, started_at, status)
                VALUES (?, ?, 'running')
                """,
                (int(source_id), now),
            )
            run_id = int(cur.lastrowid or 0)
        return run_id

    def finish_import_run(
        self,
        run_id: int,
        *,
        status: str,
        records_ingested: int = 0,
        records_failed: int = 0,
        error_message: str | None = None,
    ) -> None:
        """Close an import run and update the parent ``last_run_at``."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE import_runs
                SET ended_at = ?, status = ?, records_ingested = ?,
                    records_failed = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    now,
                    status,
                    int(records_ingested),
                    int(records_failed),
                    error_message,
                    int(run_id),
                ),
            )
            conn.execute(
                """
                UPDATE import_sources SET last_run_at = ?
                WHERE id = (SELECT source_id FROM import_runs WHERE id = ?)
                """,
                (now, int(run_id)),
            )

    def list_import_runs(self, source_id: int, limit: int = 20) -> list[ImportRun]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_id, started_at, ended_at, status,
                       records_ingested, records_failed, error_message
                FROM import_runs
                WHERE source_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (int(source_id), safe_limit),
            ).fetchall()
        return [ImportRun(**dict(row)) for row in rows]

    def purge_usage_events_older_than(self, retention_days: int) -> int:
        """Delete usage_events rows whose ISO timestamp predates the cutoff.

        Returns the number of rows removed. A non-positive ``retention_days``
        disables the purge and returns 0 without touching the table.
        """
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM usage_events WHERE timestamp < ?", (cutoff,))
        return int(cur.rowcount or 0)

    def log_usage_event(
        self,
        request_id: str,
        endpoint: str,
        method: str,
        status_code: int,
        api_key_id: str | None,
        subject: str,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events(request_id, timestamp, endpoint, method, status_code, api_key_id, subject, latency_ms, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    datetime.now(timezone.utc).isoformat(),
                    endpoint,
                    method,
                    status_code,
                    api_key_id,
                    subject,
                    latency_ms,
                    error_code,
                ),
            )

    def list_recent_usage_events(self, limit: int = 100, offset: int = 0) -> list[UsageEvent]:
        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, endpoint, method, status_code, api_key_id, subject, latency_ms, error_code
                FROM usage_events
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            ).fetchall()
        return [UsageEvent(**dict(row)) for row in rows]

    def query_usage_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        endpoint: str | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        since: str | None = None,
        until: str | None = None,
        key_id: str | None = None,
    ) -> tuple[list[UsageEvent], int]:
        """Filterable log query backing ``GET /admin/v1/logs``.

        Returns ``(page, total_matching)``. Every filter is optional;
        an omitted filter is not applied. Time filters accept the same
        ISO-8601 string format we write to the column, making
        ``since=2026-04-20T00:00:00+00:00`` a drop-in match.
        """
        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        where: list[str] = []
        params: list[object] = []
        if endpoint:
            where.append("endpoint = ?")
            params.append(endpoint)
        if status_min is not None:
            where.append("status_code >= ?")
            params.append(int(status_min))
        if status_max is not None:
            where.append("status_code <= ?")
            params.append(int(status_max))
        if since:
            where.append("timestamp >= ?")
            params.append(since)
        if until:
            where.append("timestamp <= ?")
            params.append(until)
        if key_id:
            where.append("api_key_id = ?")
            params.append(key_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM usage_events {where_sql}",  # noqa: S608
                params,
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = conn.execute(
                f"""
                SELECT timestamp, endpoint, method, status_code, api_key_id, subject, latency_ms, error_code
                FROM usage_events
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                (*params, safe_limit, safe_offset),
            ).fetchall()
        return [UsageEvent(**dict(row)) for row in rows], total

    def count_usage_events(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()
        return int(row["c"]) if row else 0

    def usage_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"]
            errors = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events WHERE status_code >= 400"
            ).fetchone()["c"]
            keys = conn.execute(
                "SELECT COUNT(*) AS c FROM api_keys WHERE status = 'active'"
            ).fetchone()["c"]
        return {"events": int(total), "errors": int(errors), "active_keys": int(keys)}

    def storage_stats(self) -> dict[str, object]:
        """Aggregate row counts, schema version and on-disk size.

        Safe to expose to operators (admin-only endpoint) — no secret
        material, only structural information useful for capacity planning
        and retention sanity checks.
        """
        stats: dict[str, object] = {}
        with self._connect() as conn:
            for table in ("api_keys", "ui_sessions", "usage_events", "schema_version"):
                # `table` comes from a fixed allowlist literal; never external.
                sql = f"SELECT COUNT(*) AS c FROM {table}"  # noqa: S608
                try:
                    count = conn.execute(sql).fetchone()["c"]
                except sqlite3.OperationalError:
                    count = None
                stats[f"rows_{table}"] = int(count) if count is not None else None
            stats["schema_version"] = _schema_version(conn)
        stats["db_path"] = str(self.db_path)
        try:
            stats["db_size_bytes"] = self.db_path.stat().st_size
        except OSError:
            stats["db_size_bytes"] = None
        return stats
