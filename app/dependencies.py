from __future__ import annotations

import logging
import sys
import threading

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.auth.api_keys import ApiKeyManager
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.mappers.schema_mapper import MappingHealthService, SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import InMemoryRateLimiter
from app.runtime_paths import get_state_db_path, resolve_bootstrap_admin_key
from app.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class Container:
    def __init__(self) -> None:
        self._reload_lock = threading.RLock()
        self.config_manager = ConfigManager(require_existing=False)
        config = self.config_manager.config

        self.store = SQLiteStore(get_state_db_path(config.storage.sqlite_path))
        self.store.initialize()

        bootstrap_key, generated = resolve_bootstrap_admin_key(config.auth.bootstrap_admin_key)
        if generated:
            # Emit once to stderr so the operator can capture the one-time secret.
            sys.stderr.write(
                "[EGG-API] Generated a bootstrap admin key (saved to the sidecar file). "
                "Set EGG_BOOTSTRAP_ADMIN_KEY to pin it across restarts.\n"
            )
        self.api_keys = ApiKeyManager(self.store, bootstrap_key)
        self.rate_limiter = InMemoryRateLimiter(
            max_requests=config.rate_limit.public_max_requests,
            window_seconds=config.rate_limit.public_window_seconds,
        )
        self.login_rate_limiter = InMemoryRateLimiter(
            max_requests=config.rate_limit.admin_login_max_requests,
            window_seconds=config.rate_limit.admin_login_window_seconds,
        )
        self.mapper = SchemaMapper(config)
        self.mapping_health = MappingHealthService()
        self.policy = QueryPolicyEngine(config)
        self.adapter = ElasticsearchAdapter(
            config.backend.url,
            config.backend.index,
            timeout_seconds=config.backend.timeout_seconds,
            max_retries=config.backend.max_retries,
            retry_backoff_seconds=config.backend.retry_backoff_seconds,
            max_buckets_per_facet=config.profiles[config.security_profile].max_buckets_per_facet,
        )

    def reload(self, config: AppConfig) -> None:
        with self._reload_lock:
            self.config_manager.save(config)
            self.store = SQLiteStore(get_state_db_path(config.storage.sqlite_path))
            self.store.initialize()
            bootstrap_key, _ = resolve_bootstrap_admin_key(config.auth.bootstrap_admin_key)
            self.api_keys = ApiKeyManager(self.store, bootstrap_key)
            self.rate_limiter = InMemoryRateLimiter(
                max_requests=config.rate_limit.public_max_requests,
                window_seconds=config.rate_limit.public_window_seconds,
            )
            self.login_rate_limiter = InMemoryRateLimiter(
                max_requests=config.rate_limit.admin_login_max_requests,
                window_seconds=config.rate_limit.admin_login_window_seconds,
            )
            self.mapper = SchemaMapper(config)
            self.policy = QueryPolicyEngine(config)
            previous_adapter = self.adapter
            self.adapter = ElasticsearchAdapter(
                config.backend.url,
                config.backend.index,
                timeout_seconds=config.backend.timeout_seconds,
                max_retries=config.backend.max_retries,
                retry_backoff_seconds=config.backend.retry_backoff_seconds,
                max_buckets_per_facet=config.profiles[
                    config.security_profile
                ].max_buckets_per_facet,
            )
            # Release the old httpx client + its connection pool. If a handler
            # was still holding a reference it keeps working until it drops it,
            # but we stop leaking sockets/FDs across reloads.
            if previous_adapter is not None:
                try:
                    previous_adapter.client.close()
                except Exception:
                    logger.exception("previous_adapter_close_failed")


container = Container()
