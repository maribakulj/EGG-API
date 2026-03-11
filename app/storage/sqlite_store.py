from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ApiKeyRecord:
    key_id: str
    status: str
    created_at: str
    prefix: str


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
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

                CREATE TABLE IF NOT EXISTS quota_config (
                    scope TEXT PRIMARY KEY,
                    max_requests INTEGER NOT NULL,
                    window_seconds INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quota_counters (
                    subject TEXT PRIMARY KEY,
                    window_started_at INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
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
                """
            )

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def ensure_admin_key(self, key: str, key_id: str = "admin") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO api_keys(key_id, key_hash, prefix, status, created_at)
                VALUES (?, ?, ?, 'active', ?)
                """,
                (key_id, self._hash_key(key), key[:8], now),
            )

    def create_api_key(self, key_id: str) -> tuple[str, ApiKeyRecord]:
        secret = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_keys(key_id, key_hash, prefix, status, created_at)
                VALUES (?, ?, ?, 'active', ?)
                """,
                (key_id, self._hash_key(secret), secret[:8], now),
            )
        return secret, ApiKeyRecord(key_id=key_id, status="active", created_at=now, prefix=secret[:8])

    def list_api_keys(self) -> list[ApiKeyRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_id, status, created_at, prefix FROM api_keys ORDER BY key_id"
            ).fetchall()
        return [ApiKeyRecord(**dict(row)) for row in rows]

    def set_key_status(self, secret_or_key_id: str, status: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE api_keys
                SET status = ?
                WHERE key_hash = ? OR key_id = ?
                """,
                (status, self._hash_key(secret_or_key_id), secret_or_key_id),
            )
        return cur.rowcount > 0

    def validate_api_key(self, secret: str | None) -> ApiKeyRecord | None:
        if not secret:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT key_id, status, created_at, prefix FROM api_keys
                WHERE key_hash = ?
                """,
                (self._hash_key(secret),),
            ).fetchone()
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

    def get_quota(self, scope: str, default_max: int, default_window: int) -> tuple[int, int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT max_requests, window_seconds FROM quota_config WHERE scope = ?", (scope,)
            ).fetchone()
            if row:
                return int(row["max_requests"]), int(row["window_seconds"])

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO quota_config(scope, max_requests, window_seconds, updated_at) VALUES (?, ?, ?, ?)",
                (scope, default_max, default_window, now),
            )
            return default_max, default_window

    def allow_subject(self, subject: str, scope: str, default_max: int, default_window: int, now_ts: int) -> bool:
        max_requests, window_seconds = self.get_quota(scope, default_max, default_window)
        window_start = now_ts - (now_ts % window_seconds)
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            row = conn.execute(
                "SELECT window_started_at, count FROM quota_counters WHERE subject = ?",
                (subject,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO quota_counters(subject, window_started_at, count, updated_at) VALUES (?, ?, 1, ?)",
                    (subject, window_start, now),
                )
                return True

            existing_window = int(row["window_started_at"])
            count = int(row["count"])
            if existing_window != window_start:
                conn.execute(
                    "UPDATE quota_counters SET window_started_at = ?, count = 1, updated_at = ? WHERE subject = ?",
                    (window_start, now, subject),
                )
                return True

            if count >= max_requests:
                return False

            conn.execute(
                "UPDATE quota_counters SET count = count + 1, updated_at = ? WHERE subject = ?",
                (now, subject),
            )
            return True

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

    def usage_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"]
            errors = conn.execute("SELECT COUNT(*) AS c FROM usage_events WHERE status_code >= 400").fetchone()["c"]
            keys = conn.execute("SELECT COUNT(*) AS c FROM api_keys WHERE status = 'active'").fetchone()["c"]
        return {"events": int(total), "errors": int(errors), "active_keys": int(keys)}
