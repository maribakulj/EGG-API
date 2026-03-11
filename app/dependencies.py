from __future__ import annotations

from pathlib import Path

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.auth.api_keys import ApiKeyManager
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.mappers.schema_mapper import MappingHealthService, SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import InMemoryRateLimiter


class Container:
    def __init__(self) -> None:
        self.config_manager = ConfigManager(Path("examples/config.yaml"))
        config = self.config_manager.config
        self.api_keys = ApiKeyManager()
        self.rate_limiter = InMemoryRateLimiter()
        self.mapper = SchemaMapper(config)
        self.mapping_health = MappingHealthService()
        self.policy = QueryPolicyEngine(config)
        self.adapter = ElasticsearchAdapter(config.backend.url, config.backend.index)

    def reload(self, config: AppConfig) -> None:
        self.config_manager.save(config)
        self.mapper = SchemaMapper(config)
        self.policy = QueryPolicyEngine(config)
        self.adapter = ElasticsearchAdapter(config.backend.url, config.backend.index)


container = Container()
