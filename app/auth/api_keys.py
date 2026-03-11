from __future__ import annotations

from dataclasses import dataclass

from app.storage.sqlite_store import ApiKeyRecord, SQLiteStore


@dataclass
class ApiKey:
    key_id: str
    key: str
    created_at: str
    revoked: bool = False


class ApiKeyManager:
    def __init__(self, store: SQLiteStore, bootstrap_admin_key: str) -> None:
        self.store = store
        self.store.ensure_admin_key(bootstrap_admin_key, key_id="admin")
        self.default_admin_key = bootstrap_admin_key

    def create(self, key_id: str) -> ApiKey:
        key, meta = self.store.create_api_key(key_id)
        return ApiKey(key_id=meta.key_id, key=key, created_at=meta.created_at, revoked=False)

    def list_keys(self) -> list[ApiKeyRecord]:
        return self.store.list_api_keys()

    def revoke(self, key: str) -> bool:
        return self.store.set_key_status(key, "revoked")

    def suspend(self, key: str) -> bool:
        return self.store.set_key_status(key, "suspended")

    def validate(self, key: str | None) -> bool:
        return self.store.validate_api_key(key) is not None

    def get_identity(self, key: str | None) -> ApiKeyRecord | None:
        return self.store.validate_api_key(key)
