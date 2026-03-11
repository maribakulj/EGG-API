from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ApiKey:
    key_id: str
    key: str
    created_at: str
    revoked: bool = False


class ApiKeyManager:
    def __init__(self) -> None:
        self._keys: dict[str, ApiKey] = {}
        default = self.create("admin")
        self.default_admin_key = default.key

    def create(self, key_id: str) -> ApiKey:
        key = secrets.token_urlsafe(24)
        entry = ApiKey(key_id=key_id, key=key, created_at=datetime.now(timezone.utc).isoformat())
        self._keys[key] = entry
        return entry

    def list_keys(self) -> list[ApiKey]:
        return list(self._keys.values())

    def revoke(self, key: str) -> bool:
        entry = self._keys.get(key)
        if not entry:
            return False
        entry.revoked = True
        return True

    def validate(self, key: str | None) -> bool:
        if not key:
            return False
        entry = self._keys.get(key)
        return bool(entry and not entry.revoked)
