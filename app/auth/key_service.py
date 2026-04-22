"""Shared API-key lifecycle logic used by both the admin UI and REST API.

Before Sprint 13 the admin HTML routes poked directly at
``container.api_keys`` and ``container.store`` and duplicated the label
regex.  Now both the Jinja form and the ``/admin/v1/keys`` JSON API go
through :class:`ApiKeyService`, so rules like "rotation invalidates
every active UI session for the key_id" live in exactly one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.auth.api_keys import ApiKey, ApiKeyManager
from app.errors import AppError
from app.storage.sqlite_store import ApiKeyRecord, SQLiteStore

# Public label: URL-safe subset so nothing has to be escaped in the UI or
# in a path segment.  Range chosen to cover human-readable labels
# ("partner-aggregator", "public_readonly_v2") without accepting free-form
# text that could collide with structured identifiers.
KEY_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

_ALLOWED_STATUS_ACTIONS = frozenset({"activate", "suspend", "revoke"})


@dataclass(frozen=True)
class KeyRotationResult:
    key_id: str
    key: str  # New raw secret — disclosed exactly once.


class ApiKeyService:
    """Orchestrates key CRUD + session invalidation.

    Uses :class:`AppError` with HTTP-friendly status codes so REST
    handlers can surface them directly; UI callers translate them into
    flash messages.
    """

    def __init__(self, manager: ApiKeyManager, store: SQLiteStore) -> None:
        self._manager = manager
        self._store = store

    # -- Validation ----------------------------------------------------------
    @staticmethod
    def validate_label(key_id: str) -> None:
        if not KEY_ID_PATTERN.match(key_id or ""):
            raise AppError(
                "invalid_parameter",
                "Key label must be 1-64 characters and match [a-zA-Z0-9_.-]",
                {"key_id": key_id},
                status_code=400,
            )

    # -- Reads ---------------------------------------------------------------
    def list_keys(self) -> list[ApiKeyRecord]:
        return self._manager.list_keys()

    def get_key(self, key_id: str) -> ApiKeyRecord:
        self.validate_label(key_id)
        for record in self._manager.list_keys():
            if record.key_id == key_id:
                return record
        raise AppError(
            "not_found",
            f"Unknown key label: {key_id}",
            {"key_id": key_id},
            status_code=404,
        )

    # -- Writes --------------------------------------------------------------
    def create(self, key_id: str) -> ApiKey:
        self.validate_label(key_id)
        # Duplicate labels would raise sqlite3.IntegrityError; surface a
        # clean 409 instead of leaking the SQL exception.
        for existing in self._manager.list_keys():
            if existing.key_id == key_id:
                raise AppError(
                    "conflict",
                    f"Key label already exists: {key_id}",
                    {"key_id": key_id},
                    status_code=409,
                )
        return self._manager.create(key_id)

    def rotate(self, key_id: str) -> KeyRotationResult:
        self.validate_label(key_id)
        new_secret = self._manager.rotate(key_id)
        if new_secret is None:
            raise AppError(
                "not_found",
                f"Unknown key label: {key_id}",
                {"key_id": key_id},
                status_code=404,
            )
        # Rotation makes the stored hash change: any session signed under
        # the previous secret is now on borrowed time, so we kick them
        # explicitly to avoid a window where the old cookie still works.
        self._store.invalidate_sessions_for_key_id(key_id)
        return KeyRotationResult(key_id=key_id, key=new_secret)

    def set_status(self, key_id: str, action: str) -> ApiKeyRecord:
        """Transition a key to ``activate`` / ``suspend`` / ``revoke``.

        ``revoke`` and ``suspend`` also drop any active UI session tied to
        the ``key_id`` so the operator-initiated lockout is immediate.
        """
        self.validate_label(key_id)
        if action not in _ALLOWED_STATUS_ACTIONS:
            raise AppError(
                "invalid_parameter",
                f"action must be one of {sorted(_ALLOWED_STATUS_ACTIONS)}",
                {"action": action},
                status_code=400,
            )
        if action == "activate":
            ok = self._manager.activate_by_key_id(key_id)
        elif action == "suspend":
            ok = self._manager.suspend_by_key_id(key_id)
        else:  # revoke
            ok = self._manager.revoke_by_key_id(key_id)

        if not ok:
            raise AppError(
                "not_found",
                f"Unknown key label: {key_id}",
                {"key_id": key_id},
                status_code=404,
            )
        if action in {"suspend", "revoke"}:
            self._store.invalidate_sessions_for_key_id(key_id)
        # Return the up-to-date record so the caller can display the new
        # status without re-querying the full list.
        return self.get_key(key_id)
