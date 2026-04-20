from __future__ import annotations

import contextlib
import hashlib
import hmac
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
                "INSERT INTO ui_sessions(token, key_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, key_id, now.isoformat(), expires_at),
            )
        return token

    def get_ui_session_key_id(self, token: str | None) -> str | None:
        if not token:
            return None
        token_hash = self._hash_session_token(token)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key_id, expires_at FROM ui_sessions WHERE token = ?",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            expires_at = row["expires_at"]
            if expires_at is not None and expires_at <= now_iso:
                conn.execute("DELETE FROM ui_sessions WHERE token = ?", (token_hash,))
                return None
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
