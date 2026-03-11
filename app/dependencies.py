from __future__ import annotations

import os
from pathlib import Path

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.auth.api_keys import ApiKeyManager
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.mappers.schema_mapper import MappingHealthService, SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import PersistentRateLimiter
from app.storage.sqlite_store import SQLiteStore


class Container:
    def __init__(self) -> None:
        self.config_manager = ConfigManager()
        config = self.config_manager.config
        db_path = Path(os.getenv("PISCO_STATE_DB_PATH", config.storage.sqlite_path))
        self.store = SQLiteStore(db_path)
        self.store.initialize()
        bootstrap_key = os.getenv("PISCO_BOOTSTRAP_ADMIN_KEY", config.auth.bootstrap_admin_key)
        self.api_keys = ApiKeyManager(self.store, bootstrap_key)
        self.rate_limiter = PersistentRateLimiter(self.store)
        self.mapper = SchemaMapper(config)
        self.mapping_health = MappingHealthService()
        self.policy = QueryPolicyEngine(config)
        self.adapter = ElasticsearchAdapter(config.backend.url, config.backend.index)

    def reload(self, config: AppConfig) -> None:
        self.config_manager.save(config)
        db_path = Path(os.getenv("PISCO_STATE_DB_PATH", config.storage.sqlite_path))
        self.store = SQLiteStore(db_path)
        self.store.initialize()
        bootstrap_key = os.getenv("PISCO_BOOTSTRAP_ADMIN_KEY", config.auth.bootstrap_admin_key)
        self.api_keys = ApiKeyManager(self.store, bootstrap_key)
        self.rate_limiter = PersistentRateLimiter(self.store)
        self.mapper = SchemaMapper(config)
        self.policy = QueryPolicyEngine(config)
        self.adapter = ElasticsearchAdapter(config.backend.url, config.backend.index)


container = Container()
