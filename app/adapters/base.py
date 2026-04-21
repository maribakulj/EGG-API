"""Backend adapter Protocol.

Every backend (Elasticsearch, OpenSearch, future Solr…) plugs into EGG-API
through the :class:`BackendAdapter` shape. The Protocol is runtime-checkable
so tests can assert conformance; static checkers see it as structural
typing (no inheritance required). Keeping the contract here in
``app.adapters`` — not in the concrete adapter module — lets new
implementations live in their own file without creating an import cycle
with the factory.

Adding a new backend:
    1. Implement the methods below (raising :class:`~app.errors.AppError`
       for typed failures; transient HTTP errors flow through the retry
       loop).
    2. Register it in :mod:`app.adapters.factory` under a new
       ``BackendType`` literal.
    3. Add a test module that instantiates the adapter behind
       ``httpx.MockTransport`` and asserts ``isinstance(a, BackendAdapter)``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.schemas.query import NormalizedQuery


@runtime_checkable
class BackendAdapter(Protocol):
    """Read-only search backend the public API talks to."""

    # Identification / liveness -------------------------------------------
    def detect(self) -> dict[str, Any]:
        """Return the backend's version/capability metadata.

        Used by ``/admin/v1/setup/detect`` at bootstrap and by the adapter
        factory for sanity checks. Implementations should reject versions
        below the supported floor with
        ``AppError('unsupported_backend_version', ..., 503)``.
        """

    def health(self) -> dict[str, Any]:
        """Return the backend's cluster/readiness payload.

        Exposed only through the admin-authenticated ``/v1/readyz`` and
        the admin dashboard; never to anonymous callers.
        """

    def list_sources(self) -> list[str]:
        """Enumerate the indexes/collections exposed to the public API."""

    def scan_fields(self) -> dict[str, Any]:
        """Return the backend field mapping used by ``/admin/v1/setup/scan-fields``."""

    # Query translation ---------------------------------------------------
    def translate_query(self, query: NormalizedQuery) -> dict[str, Any]:
        """Translate a ``NormalizedQuery`` into the backend's DSL.

        Concrete adapters MAY expose additional keyword-only tuning knobs
        (e.g. ``size_override``, ``max_buckets_per_facet``) on top of this
        minimum signature; callers on the Protocol path must not rely on
        them.
        """

    # Read operations -----------------------------------------------------
    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        """Run a full-text search and return the raw backend response."""

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        """Fetch a single record's ``_source`` payload, or ``None`` if missing."""

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        """Aggregations-only search used by the ``/v1/facets`` endpoint."""

    def suggest(self, prefix: str, limit: int = 10) -> list[str]:
        """Return completion candidates for ``prefix``.

        Implementations that don't support a completion surface SHOULD
        return an empty list; callers must not treat that as a failure.
        Raising ``AppError('not_implemented', 501)`` is also acceptable
        when the backend cannot provide the capability at all.
        """

    # Response post-processing -------------------------------------------
    @staticmethod
    def extract_facets(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        """Extract the facet payload from a raw search response.

        Kept as a staticmethod so the public ``/v1/search`` handler can
        reuse it against any backend without instantiating an adapter.
        """
