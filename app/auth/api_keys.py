from __future__ import annotations

from dataclasses import dataclass

from app.storage.sqlite_store import ApiKeyRecord, SQLiteStore


@dataclass
class ApiKey:
    key_id: str
    key: str
    created_at: str


class ApiKeyManager:
    def __init__(self, store: SQLiteStore, bootstrap_admin_key: str) -> None:
        self._store = store
        self._store.ensure_admin_key(bootstrap_admin_key)
        self.default_admin_key = bootstrap_admin_key

    def create(self, key_id: str) -> ApiKey:
        if not key_id:
            raise ValueError("Key label is required")
        secret, record = self._store.create_api_key(key_id)
        return ApiKey(key_id=record.key_id, key=secret, created_at=record.created_at)

    def list_keys(self) -> list[ApiKeyRecord]:
        return self._store.list_api_keys()

    # --- By key_id (public label, safe to log) ------------------------------
    def revoke_by_key_id(self, key_id: str) -> bool:
        return self._store.set_key_status_by_key_id(key_id, "revoked")

    def suspend_by_key_id(self, key_id: str) -> bool:
        return self._store.set_key_status_by_key_id(key_id, "suspended")

    def activate_by_key_id(self, key_id: str) -> bool:
        return self._store.set_key_status_by_key_id(key_id, "active")

    # --- By secret (only use when the operator holds the raw key) ----------
    def revoke_by_secret(self, secret: str) -> bool:
        return self._store.set_key_status_by_secret(secret, "revoked")

    def suspend_by_secret(self, secret: str) -> bool:
        return self._store.set_key_status_by_secret(secret, "suspended")

    def activate_by_secret(self, secret: str) -> bool:
        return self._store.set_key_status_by_secret(secret, "active")

    # --- Legacy shims (accept either shape; prefer the explicit APIs). ----
    def revoke(self, key_id_or_secret: str) -> bool:
        return self._set_status_either(key_id_or_secret, "revoked")

    def suspend(self, key_id_or_secret: str) -> bool:
        return self._set_status_either(key_id_or_secret, "suspended")

    def activate(self, key_id_or_secret: str) -> bool:
        return self._set_status_either(key_id_or_secret, "active")

    def _set_status_either(self, value: str, status: str) -> bool:
        if self._store.set_key_status_by_key_id(value, status):
            return True
        return self._store.set_key_status_by_secret(value, status)

    def rotate(self, key_id: str) -> str | None:
        """Regenerate the raw secret behind ``key_id``. Returns the new value.

        When rotating the bootstrap admin key, keep ``default_admin_key`` in
        sync so that subsequent ``ensure_admin_key`` calls (e.g. on Container
        reload) don't resurrect the old value.
        """
        new_secret = self._store.rotate_api_key(key_id)
        if new_secret and key_id == "admin":
            self.default_admin_key = new_secret
        return new_secret

    # --- Validation --------------------------------------------------------
    def validate(self, key: str | None) -> bool:
        return self._store.validate_api_key(key) is not None

    def get_identity(self, key: str | None) -> ApiKeyRecord | None:
        return self._store.validate_api_key(key)
