from __future__ import annotations

import time

from app.storage.sqlite_store import SQLiteStore


class PersistentRateLimiter:
    def __init__(self, store: SQLiteStore, scope: str = "public", max_requests: int = 60, window_seconds: int = 60) -> None:
        self.store = store
        self.scope = scope
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def allow(self, subject: str) -> bool:
        now_ts = int(time.time())
        return self.store.allow_subject(subject, self.scope, self.max_requests, self.window_seconds, now_ts)
