from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from app.adapters.base import BackendAdapter
from app.adapters.factory import build_adapter
from app.auth.api_keys import ApiKeyManager
from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.mappers.schema_mapper import MappingHealthService, SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import InMemoryRateLimiter
from app.rate_limit.redis_limiter import RedisRateLimiter, build_rate_limiter
from app.runtime_paths import get_state_db_path, resolve_bootstrap_admin_key
from app.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


# Type alias shared with the factory: both rate-limiter flavors expose
# ``allow(subject)`` + ``max_requests`` + ``window_seconds`` but have no
# common superclass.
_RateLimiter = InMemoryRateLimiter | RedisRateLimiter


@dataclass(frozen=True)
class ContainerState:
    """Immutable snapshot of every service the request path depends on.

    ``Container.reload()`` builds a new ``ContainerState`` in full, then
    swaps the reference under a lock. Readers who follow the
    ``Container.state`` pointer see either the old snapshot or the new
    one — never a half-applied mix of both. Pre-Sprint-10 the fields
    were mutated one by one on the ``Container`` itself, which let a
    lock-free reader observe e.g. ``new_store`` + ``old_api_keys``.
    """

    config_manager: ConfigManager
    store: SQLiteStore
    api_keys: ApiKeyManager
    rate_limiter: _RateLimiter
    login_rate_limiter: _RateLimiter
    mapper: SchemaMapper
    mapping_health: MappingHealthService
    policy: QueryPolicyEngine
    adapter: BackendAdapter
    # Background-purge snapshot. Lives on the container so /admin/v1/storage/stats
    # never reaches back into ``app.main`` (Sprint 10 cleanup).
    last_purge_state: dict[str, Any] = field(
        default_factory=lambda: {
            "last_run_at": None,
            "sessions_purged": 0,
            "events_purged": 0,
            "errors": 0,
        }
    )


def _build_state(
    config_manager: ConfigManager,
    *,
    previous: ContainerState | None = None,
) -> ContainerState:
    """Compose a fresh state snapshot from a config."""
    config = config_manager.config
    store = SQLiteStore(get_state_db_path(config.storage.sqlite_path))
    store.initialize()

    bootstrap_key, generated = resolve_bootstrap_admin_key(config.auth.bootstrap_admin_key)
    if generated and previous is None:
        # Only emit on the initial boot — reloads never re-generate.
        sys.stderr.write(
            "[EGG-API] Generated a bootstrap admin key (saved to the sidecar file). "
            "Set EGG_BOOTSTRAP_ADMIN_KEY to pin it across restarts.\n"
        )
    return ContainerState(
        config_manager=config_manager,
        store=store,
        api_keys=ApiKeyManager(store, bootstrap_key),
        rate_limiter=build_rate_limiter(
            max_requests=config.rate_limit.public_max_requests,
            window_seconds=config.rate_limit.public_window_seconds,
            scope="public",
        ),
        login_rate_limiter=build_rate_limiter(
            max_requests=config.rate_limit.admin_login_max_requests,
            window_seconds=config.rate_limit.admin_login_window_seconds,
            scope="admin_login",
        ),
        mapper=SchemaMapper(config),
        mapping_health=MappingHealthService(),
        policy=QueryPolicyEngine(config),
        adapter=build_adapter(config),
        # Reuse the previous purge snapshot across reloads: a config
        # reload is not a reason to pretend no purge ever ran.
        last_purge_state=previous.last_purge_state
        if previous is not None
        else {
            "last_run_at": None,
            "sessions_purged": 0,
            "events_purged": 0,
            "errors": 0,
        },
    )


class Container:
    """Indirection over a ``ContainerState`` snapshot.

    Exposes the formerly-direct attributes (``store``, ``api_keys``…) as
    properties that pull from the current ``state`` reference. Swapping
    ``state`` under a lock makes ``Container.reload()`` atomic from any
    lock-free reader's point of view.

    The attribute API is kept so existing call sites (``container.store``,
    ``container.adapter``…) keep working unchanged.
    """

    def __init__(self) -> None:
        self._reload_lock = threading.RLock()
        config_manager = ConfigManager(require_existing=False)
        self._state: ContainerState = _build_state(config_manager)

    # --- Atomic reference to the current state -------------------------
    @property
    def state(self) -> ContainerState:
        return self._state

    # --- Pass-through attribute access for back-compat -----------------
    @property
    def config_manager(self) -> ConfigManager:
        return self._state.config_manager

    @property
    def store(self) -> SQLiteStore:
        return self._state.store

    @store.setter
    def store(self, value: SQLiteStore) -> None:
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=value,
            api_keys=self._state.api_keys,
            rate_limiter=self._state.rate_limiter,
            login_rate_limiter=self._state.login_rate_limiter,
            mapper=self._state.mapper,
            mapping_health=self._state.mapping_health,
            policy=self._state.policy,
            adapter=self._state.adapter,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def api_keys(self) -> ApiKeyManager:
        return self._state.api_keys

    @api_keys.setter
    def api_keys(self, value: ApiKeyManager) -> None:
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=self._state.store,
            api_keys=value,
            rate_limiter=self._state.rate_limiter,
            login_rate_limiter=self._state.login_rate_limiter,
            mapper=self._state.mapper,
            mapping_health=self._state.mapping_health,
            policy=self._state.policy,
            adapter=self._state.adapter,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def rate_limiter(self) -> _RateLimiter:
        return self._state.rate_limiter

    @rate_limiter.setter
    def rate_limiter(self, value: _RateLimiter) -> None:
        # Tests swap the limiter directly via container.rate_limiter =
        # InMemoryRateLimiter(...). Keep that affordance working; it
        # mutates the current snapshot rather than building a new one.
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=self._state.store,
            api_keys=self._state.api_keys,
            rate_limiter=value,
            login_rate_limiter=self._state.login_rate_limiter,
            mapper=self._state.mapper,
            mapping_health=self._state.mapping_health,
            policy=self._state.policy,
            adapter=self._state.adapter,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def login_rate_limiter(self) -> _RateLimiter:
        return self._state.login_rate_limiter

    @login_rate_limiter.setter
    def login_rate_limiter(self, value: _RateLimiter) -> None:
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=self._state.store,
            api_keys=self._state.api_keys,
            rate_limiter=self._state.rate_limiter,
            login_rate_limiter=value,
            mapper=self._state.mapper,
            mapping_health=self._state.mapping_health,
            policy=self._state.policy,
            adapter=self._state.adapter,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def mapper(self) -> SchemaMapper:
        return self._state.mapper

    @mapper.setter
    def mapper(self, value: SchemaMapper) -> None:
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=self._state.store,
            api_keys=self._state.api_keys,
            rate_limiter=self._state.rate_limiter,
            login_rate_limiter=self._state.login_rate_limiter,
            mapper=value,
            mapping_health=self._state.mapping_health,
            policy=self._state.policy,
            adapter=self._state.adapter,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def mapping_health(self) -> MappingHealthService:
        return self._state.mapping_health

    @property
    def policy(self) -> QueryPolicyEngine:
        return self._state.policy

    @policy.setter
    def policy(self, value: QueryPolicyEngine) -> None:
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=self._state.store,
            api_keys=self._state.api_keys,
            rate_limiter=self._state.rate_limiter,
            login_rate_limiter=self._state.login_rate_limiter,
            mapper=self._state.mapper,
            mapping_health=self._state.mapping_health,
            policy=value,
            adapter=self._state.adapter,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def adapter(self) -> BackendAdapter:
        return self._state.adapter

    @adapter.setter
    def adapter(self, value: BackendAdapter) -> None:
        self._state = ContainerState(
            config_manager=self._state.config_manager,
            store=self._state.store,
            api_keys=self._state.api_keys,
            rate_limiter=self._state.rate_limiter,
            login_rate_limiter=self._state.login_rate_limiter,
            mapper=self._state.mapper,
            mapping_health=self._state.mapping_health,
            policy=self._state.policy,
            adapter=value,
            last_purge_state=self._state.last_purge_state,
        )

    @property
    def last_purge_state(self) -> dict[str, Any]:
        return self._state.last_purge_state

    # --- Atomic reload -------------------------------------------------
    def reload(self, config: AppConfig) -> None:
        with self._reload_lock:
            previous = self._state
            previous.config_manager.save(config)
            new_state = _build_state(previous.config_manager, previous=previous)
            # Single assignment: readers see either ``previous`` or
            # ``new_state`` — nothing in between.
            self._state = new_state

            # Release the orphaned httpx client so we don't leak FDs.
            client = getattr(previous.adapter, "client", None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    logger.exception("previous_adapter_close_failed")


container = Container()


def get_container(request: Request) -> Container:
    """FastAPI ``Depends`` flavor of the module-level singleton.

    Prefers ``request.app.state.container`` so tests that swap the
    container on a fresh FastAPI instance don't leak across workers.
    Falls back to the module-level singleton for callers that import
    this helper outside of a request context (CLI, scripts).
    """
    state_container = getattr(request.app.state, "container", None)
    if isinstance(state_container, Container):
        return state_container
    return container
