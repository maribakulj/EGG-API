"""Adapter factory.

Looks at ``backend.type`` and hands back an instance of the matching
concrete adapter. Every branch returns something that satisfies
:class:`~app.adapters.base.BackendAdapter`; the factory is the single
place where a new backend needs to be wired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.base import BackendAdapter
from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.adapters.opensearch.adapter import OpenSearchAdapter

if TYPE_CHECKING:  # pragma: no cover - type-only
    from app.config.models import AppConfig


def build_adapter(config: AppConfig) -> BackendAdapter:
    """Return a fully-configured adapter for the active backend type."""
    backend = config.backend
    profile = config.profiles[config.security_profile]
    # Explicit kwargs (not **dict-splat): keeps mypy's overload resolution
    # happy and makes the constructor signature easy to grep for when
    # a new knob lands.
    if backend.type == "elasticsearch":
        return ElasticsearchAdapter(
            backend.url,
            backend.index,
            timeout_seconds=backend.timeout_seconds,
            max_retries=backend.max_retries,
            retry_backoff_seconds=backend.retry_backoff_seconds,
            retry_backoff_cap_seconds=backend.retry_backoff_cap_seconds,
            retry_deadline_seconds=backend.retry_deadline_seconds,
            max_buckets_per_facet=profile.max_buckets_per_facet,
            auth_config=backend.auth,
        )
    if backend.type == "opensearch":
        return OpenSearchAdapter(
            backend.url,
            backend.index,
            timeout_seconds=backend.timeout_seconds,
            max_retries=backend.max_retries,
            retry_backoff_seconds=backend.retry_backoff_seconds,
            retry_backoff_cap_seconds=backend.retry_backoff_cap_seconds,
            retry_deadline_seconds=backend.retry_deadline_seconds,
            max_buckets_per_facet=profile.max_buckets_per_facet,
            auth_config=backend.auth,
        )
    # BackendType is a Literal, so this branch is unreachable at runtime;
    # keeping the raise makes the factory robust to future additions to
    # the Literal that forget to update this file.
    raise ValueError(f"Unsupported backend.type: {backend.type!r}")
