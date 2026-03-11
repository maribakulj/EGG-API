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

    def revoke(self, secret_or_key_id: str) -> bool:
        return self._store.set_key_status(secret_or_key_id, "revoked")

    def suspend(self, secret_or_key_id: str) -> bool:
        return self._store.set_key_status(secret_or_key_id, "suspended")

    def activate(self, secret_or_key_id: str) -> bool:
        return self._store.set_key_status(secret_or_key_id, "active")

    def validate(self, key: str | None) -> bool:
        return self._store.validate_api_key(key) is not None

    def get_identity(self, key: str | None) -> ApiKeyRecord | None:
        return self._store.validate_api_key(key)
