from __future__ import annotations

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.auth.api_keys import ApiKeyManager
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.mappers.schema_mapper import MappingHealthService, SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import PersistentRateLimiter
from app.runtime_paths import get_bootstrap_admin_key, get_state_db_path
from app.storage.sqlite_store import SQLiteStore


class Container:
    def __init__(self) -> None:
        self.config_manager = ConfigManager(require_existing=True)
        config = self.config_manager.config
        self._wire(config)

    def _wire(self, config: AppConfig) -> None:
        db_path = get_state_db_path(config.storage.sqlite_path)
        self.store = SQLiteStore(db_path)
        self.store.initialize()
        bootstrap_key = get_bootstrap_admin_key(config.auth.bootstrap_admin_key)
        self.api_keys = ApiKeyManager(self.store, bootstrap_key)
        self.rate_limiter = PersistentRateLimiter(self.store)
        self.mapper = SchemaMapper(config)
        self.mapping_health = MappingHealthService()
        self.policy = QueryPolicyEngine(config)
        self.adapter = ElasticsearchAdapter(config.backend.url, config.backend.index)

    def reload(self, config: AppConfig) -> None:
        self.config_manager.save(config)
        self._wire(config)


container = Container()
