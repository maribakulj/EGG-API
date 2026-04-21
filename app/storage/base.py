"""Storage role Protocols.

The current :class:`~app.storage.sqlite_store.SQLiteStore` implements four
unrelated concerns (API keys, UI sessions, usage events, storage stats).
Splitting them into one class per role would be a heavy migration; instead
this module declares a Protocol per role. Callers that only need one role
can take the narrow Protocol as a parameter, which makes their intent
explicit and lets tests stub exactly one slice without a full
``SQLiteStore`` fixture.

``SQLiteStore`` satisfies all four Protocols today — no runtime changes —
and a future Redis/Postgres implementation can target a subset at a time.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.storage.sqlite_store import ApiKeyRecord, UsageEvent


@runtime_checkable
class KeyStore(Protocol):
    """API key persistence."""

    def ensure_admin_key(self, key: str, key_id: str = ...) -> None: ...
    def create_api_key(self, key_id: str) -> tuple[str, ApiKeyRecord]: ...
    def list_api_keys(self) -> list[ApiKeyRecord]: ...
    def validate_api_key(self, secret: str | None) -> ApiKeyRecord | None: ...
    def rotate_api_key(self, key_id: str) -> str | None: ...
    def set_key_status_by_key_id(self, key_id: str, status: str) -> bool: ...
    def set_key_status_by_secret(self, secret: str, status: str) -> bool: ...


@runtime_checkable
class SessionStore(Protocol):
    """Admin UI session persistence."""

    def create_ui_session(self, key_id: str, ttl_hours: int = ...) -> str: ...
    def get_ui_session_key_id(self, token: str | None) -> str | None: ...
    def delete_ui_session(self, token: str | None) -> None: ...
    def invalidate_sessions_for_key_id(self, key_id: str) -> int: ...
    def purge_expired_ui_sessions(self) -> int: ...


@runtime_checkable
class UsageLogger(Protocol):
    """Append-only audit log for request-level events."""

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
    ) -> None: ...
    def list_recent_usage_events(self, limit: int = ..., offset: int = ...) -> list[UsageEvent]: ...
    def count_usage_events(self) -> int: ...
    def usage_summary(self) -> dict[str, int]: ...
    def purge_usage_events_older_than(self, retention_days: int) -> int: ...


@runtime_checkable
class StatsReporter(Protocol):
    """Operator-facing storage diagnostics."""

    def storage_stats(self) -> dict[str, Any]: ...
