"""Elasticsearch adapter.

Wraps an ``httpx.Client`` (with ``follow_redirects=False`` to block SSRF via
backend redirects) and centralizes retry + typed-error behavior: transient
httpx failures and 5xx responses are retried with exponential backoff, and
exhaustion surfaces as :class:`AppError` (``backend_unavailable``, 503).
``translate_query`` builds the ES DSL in a single pass so ``search()`` and
``get_facets()`` never round-trip twice for the same call. Minor version
gating blocks Elasticsearch < 7.
"""

from __future__ import annotations

import base64
import json
import random
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from app.errors import AppError
from app.logging import get_logger
from app.metrics import backend_errors
from app.schemas.query import NormalizedQuery

if TYPE_CHECKING:  # pragma: no cover
    from app.config.models import BackendAuthConfig

logger = get_logger("egg.adapter.es")


def _encode_cursor(sort_values: list[Any]) -> str:
    """Encode an ES sort tail into an opaque URL-safe cursor token.

    The serialized shape is base64(json(sort_values)); cursors never
    expose their internals on the wire so callers cannot craft arbitrary
    ``search_after`` values.
    """
    raw = json.dumps(sort_values, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> list[Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AppError(
            "invalid_parameter",
            "cursor token is malformed",
            {"reason": "decode_failed"},
        ) from exc
    if not isinstance(value, list):
        raise AppError("invalid_parameter", "cursor token is malformed", {"reason": "shape"})
    return value


def _current_request_id() -> str | None:
    """Return the current request_id bound in the structlog contextvars, if any.

    The audit middleware binds ``request_id`` per request; adapter callers
    inside the same thread can surface it to the backend as ``X-Opaque-Id``
    without threading the value through every call site.
    """
    ctx = structlog.contextvars.get_contextvars()
    rid = ctx.get("request_id")
    return rid if isinstance(rid, str) and rid else None


def _tracing_headers() -> dict[str, str]:
    """Build the outgoing header dict for a backend call.

    Elasticsearch honors ``X-Opaque-Id`` natively: it is echoed in slow logs
    and task-management APIs, which lets operators correlate an EGG request
    with its downstream ES work.
    """
    rid = _current_request_id()
    return {"X-Opaque-Id": rid} if rid else {}


def _record_backend_error(error_code: str) -> None:
    backend_errors.labels(error_code=error_code).inc()


def _parse_major_version(version: str) -> int | None:
    if not version:
        return None
    head = version.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


_TRANSIENT_HTTP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


# Default caps on the retry loop. With retry_backoff_seconds=0.2 and
# max_retries=2, uncapped exponential would peak at 0.8 s. With higher
# retries these constants keep the worst case bounded.
_DEFAULT_RETRY_CAP_SECONDS = 5.0
_DEFAULT_RETRY_DEADLINE_SECONDS = 30.0
_JITTER_RATIO = 0.25  # ±25% of the nominal sleep


def _build_auth_headers_and_basic(
    auth_config: BackendAuthConfig | None,
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Resolve a :class:`BackendAuthConfig` into httpx-ready auth material.

    Returns ``(headers, basic_auth_tuple)``: ``headers`` holds the
    ``Authorization`` header for bearer/api_key modes (empty for
    basic/none), ``basic_auth_tuple`` is the ``(user, password)`` pair
    httpx accepts directly in ``auth=``. The split lets callers pass
    basic credentials through httpx's native handler (which masks them
    in repr/logs) instead of manually encoding them.
    """
    if auth_config is None or auth_config.mode == "none":
        return {}, None
    if auth_config.mode == "basic":
        password = auth_config.resolve_password() or ""
        username = auth_config.username or ""
        return {}, (username, password)
    if auth_config.mode == "bearer":
        token = auth_config.resolve_token() or ""
        if not token:
            return {}, None
        return {"Authorization": f"Bearer {token}"}, None
    if auth_config.mode == "api_key":
        # Elasticsearch/OpenSearch recognize ``Authorization: ApiKey <base64>``
        # for their native API-key primitive. The token is already the
        # base64-encoded ``id:api_key`` string when created via ES.
        token = auth_config.resolve_token() or ""
        if not token:
            return {}, None
        return {"Authorization": f"ApiKey {token}"}, None
    return {}, None


class ElasticsearchAdapter:
    def __init__(
        self,
        base_url: str,
        index: str,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
        retry_backoff_cap_seconds: float = _DEFAULT_RETRY_CAP_SECONDS,
        retry_deadline_seconds: float = _DEFAULT_RETRY_DEADLINE_SECONDS,
        max_buckets_per_facet: int = 20,
        auth_config: BackendAuthConfig | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.index = index
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.retry_backoff_cap_seconds = max(0.0, float(retry_backoff_cap_seconds))
        self.retry_deadline_seconds = max(0.0, float(retry_deadline_seconds))
        self.max_buckets_per_facet = max(1, int(max_buckets_per_facet))
        self._auth_headers, basic_auth = _build_auth_headers_and_basic(auth_config)
        # follow_redirects=False blocks SSRF via backend redirects to untrusted hosts.
        self.client = client or httpx.Client(
            timeout=float(timeout_seconds),
            follow_redirects=False,
            auth=basic_auth,
        )

    def _outgoing_headers(self) -> dict[str, str]:
        """Merge tracing + configured backend auth headers for one request.

        Kept as an instance method so subclasses (OpenSearch) inherit the
        same policy and so tests can swap ``_auth_headers`` on a fixture.
        """
        headers = _tracing_headers()
        if self._auth_headers:
            headers.update(self._auth_headers)
        return headers

    def _compute_sleep(self, attempt: int) -> float:
        """Exponential backoff with jitter, bounded by ``retry_backoff_cap_seconds``.

        ``attempt`` is zero-based for the first retry, so the nominal backoff
        is ``base * 2**attempt``. Jitter is ±25% uniform to spread retries
        across parallel callers and avoid synchronized thundering herd.
        """
        if self.retry_backoff_seconds <= 0:
            return 0.0
        nominal = self.retry_backoff_seconds * (2**attempt)
        capped = min(nominal, self.retry_backoff_cap_seconds)
        jitter = capped * _JITTER_RATIO
        # Non-cryptographic jitter: only spreads retry scheduling across
        # parallel callers to avoid a thundering herd.
        return max(0.0, capped + random.uniform(-jitter, jitter))  # noqa: S311

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue a request with bounded retries on transient failures/5xx.

        Retries are capped by ``max_retries`` *and* by ``retry_deadline_seconds``:
        whichever ceiling hits first terminates the loop. Raises
        :class:`AppError` ("backend_unavailable", 503) after exhaustion.
        """
        attempts = self.max_retries + 1
        last_exc: BaseException | None = None
        deadline = (
            time.monotonic() + self.retry_deadline_seconds
            if self.retry_deadline_seconds > 0
            else None
        )
        for attempt in range(attempts):
            try:
                response = self.client.request(method, url, **kwargs)
            except _TRANSIENT_HTTP_EXCEPTIONS as exc:
                last_exc = exc
                logger.warning(
                    "backend_transient_error",
                    attempt=attempt + 1,
                    attempts=attempts,
                    method=method,
                    url=url,
                    error=str(exc),
                )
                if attempt + 1 >= attempts or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    _record_backend_error("backend_unavailable")
                    raise AppError(
                        "backend_unavailable",
                        "Backend is unavailable",
                        {"reason": str(exc)},
                        503,
                    ) from exc
                time.sleep(self._compute_sleep(attempt))
                continue

            if response.status_code >= 500 and attempt + 1 < attempts:
                if deadline is not None and time.monotonic() >= deadline:
                    # Overall deadline reached; surface the last 5xx rather
                    # than retrying into certain timeout.
                    break
                logger.warning(
                    "backend_5xx",
                    attempt=attempt + 1,
                    attempts=attempts,
                    method=method,
                    url=url,
                    status_code=response.status_code,
                )
                time.sleep(self._compute_sleep(attempt))
                continue
            return response

        _record_backend_error("backend_unavailable")
        raise AppError(
            "backend_unavailable",
            "Backend is unavailable",
            {"reason": str(last_exc) if last_exc else "exhausted"},
            503,
        )

    _MIN_SUPPORTED_MAJOR_VERSION = 7

    def detect(self) -> dict[str, Any]:
        response = self._request("GET", f"{self.base_url}", headers=self._outgoing_headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _record_backend_error("backend_unavailable")
            raise AppError(
                "backend_unavailable",
                "Could not detect backend",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc

        version_info = response.json().get("version", {}) or {}
        version_number = str(version_info.get("number", ""))
        major = _parse_major_version(version_number)
        if major is not None and major < self._MIN_SUPPORTED_MAJOR_VERSION:
            _record_backend_error("unsupported_backend_version")
            raise AppError(
                "unsupported_backend_version",
                f"Elasticsearch {version_number} is not supported; "
                f"requires {self._MIN_SUPPORTED_MAJOR_VERSION}+",
                {"version": version_number, "minimum_major": self._MIN_SUPPORTED_MAJOR_VERSION},
                503,
            )
        return {"detected": True, "version": version_info}

    def health(self) -> dict[str, Any]:
        response = self._request(
            "GET", f"{self.base_url}/_cluster/health", headers=self._outgoing_headers()
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend is unavailable",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return response.json()

    def list_sources(self) -> list[str]:
        return [self.index]

    def scan_fields(self) -> dict[str, Any]:
        response = self._request(
            "GET", f"{self.base_url}/{self.index}/_mapping", headers=self._outgoing_headers()
        )
        response.raise_for_status()
        return response.json()

    def validate_mapping(self) -> dict[str, Any]:
        return {"status": "ok"}

    @staticmethod
    def _parse_sort(sort_name: str | None) -> list[dict[str, str]]:
        """Translate a symbolic sort name into an ES sort clause.

        Accepts the ``<field>_asc`` / ``<field>_desc`` convention used in
        ``config.allowed_sorts`` (e.g. ``date_desc``, ``title_asc``), plus
        the reserved ``relevance`` alias → ``_score desc``. ``None`` falls
        back to relevance so anonymous text queries keep their current
        ranking behavior.

        An ``_id asc`` tie-breaker is ALWAYS appended: ``search_after``
        cursor pagination needs a deterministic trailing key, so the very
        first page already carries a usable ``hit.sort`` tail for the
        next call — callers don't have to opt in.
        """
        primary: list[dict[str, str]]
        if not sort_name or sort_name == "relevance":
            primary = [{"_score": "desc"}]
        elif sort_name.endswith("_desc"):
            primary = [{sort_name[:-5]: "desc"}]
        elif sort_name.endswith("_asc"):
            primary = [{sort_name[:-4]: "asc"}]
        else:
            # Allowlist-validated opaque name — treat as a bare field, asc.
            primary = [{sort_name: "asc"}]
        return [*primary, {"_id": "asc"}]

    def translate_query(
        self,
        query: NormalizedQuery,
        *,
        include_aggs: bool = True,
        size_override: int | None = None,
        max_buckets_per_facet: int | None = None,
    ) -> dict[str, Any]:
        bucket_size_default = (
            self.max_buckets_per_facet if max_buckets_per_facet is None else max_buckets_per_facet
        )
        must: list[dict[str, Any]] = []
        filter_clauses: list[dict[str, Any]] = []
        if query.q:
            must.append({"simple_query_string": {"query": query.q}})
        for field, values in query.filters.items():
            filter_clauses.append({"terms": {field: values}})
        if query.has_digital is not None:
            filter_clauses.append({"term": {"has_digital": query.has_digital}})
        if query.has_iiif is not None:
            filter_clauses.append({"term": {"has_iiif": query.has_iiif}})
        # Interval-overlap semantics on ``date.start`` / ``date.end``:
        # a record whose own [start, end] window intersects the caller's
        # [date_from, date_to] is a match. Patrimonial records carry
        # messy free-form ``date`` strings ("vers 1880", "XVIIIe siècle")
        # that a flat ``range`` on ``date`` cannot query correctly, so
        # we target the normalized sub-fields of the public
        # :class:`~app.schemas.record.DateInfo`. Backends whose index
        # only carries a scalar ``date`` field need a one-line ES
        # mapping alias (``date.start``/``date.end`` pointing at their
        # existing field) to participate. Two clauses let ES optimize
        # each range independently.
        if query.date_to:
            filter_clauses.append({"range": {"date.start": {"lte": query.date_to}}})
        if query.date_from:
            filter_clauses.append({"range": {"date.end": {"gte": query.date_from}}})

        size = size_override if size_override is not None else query.page_size
        bucket_size = max(1, int(bucket_size_default))
        body: dict[str, Any] = {
            "size": size,
            "query": {"bool": {"must": must or [{"match_all": {}}], "filter": filter_clauses}},
            # Sort is emitted unconditionally: it's the precondition for
            # cursor bootstrap (first hit.sort tail → next_cursor token).
            "sort": self._parse_sort(query.sort),
        }
        if query.cursor:
            # search_after mode: stable over very deep scroll; ``from``
            # is forbidden in this mode.
            body["search_after"] = _decode_cursor(query.cursor)
        else:
            body["from"] = (query.page - 1) * query.page_size
        if include_aggs and query.facets:
            body["aggs"] = {
                facet: {"terms": {"field": facet, "size": bucket_size}} for facet in query.facets
            }
        return body

    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        payload = self.translate_query(query)
        response = self._request(
            "POST",
            f"{self.base_url}/{self.index}/_search",
            json=payload,
            headers=self._outgoing_headers(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend search failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return response.json()

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            f"{self.base_url}/{self.index}/_doc/{record_id}",
            headers=self._outgoing_headers(),
        )
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend record lookup failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        body = response.json()
        return body.get("_source")

    @staticmethod
    def extract_facets(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        """Extract facet counts from a raw search payload's ``aggregations``."""
        aggs = payload.get("aggregations", {}) or {}
        result: dict[str, dict[str, int]] = {}
        for facet, values in aggs.items():
            buckets = values.get("buckets", []) if isinstance(values, dict) else []
            result[facet] = {b["key"]: b["doc_count"] for b in buckets}
        return result

    def suggest(self, prefix: str, limit: int = 10) -> list[str]:
        """Term-level completion via ``simple_query_string`` prefix.

        We intentionally avoid the ``completion`` suggester (requires a
        dedicated mapping the operator may not have declared) and fall
        back to a prefix-match query on ``title``. Returns distinct
        titles, capped at ``limit``. Empty ``prefix`` → no suggestions.
        """
        if not prefix:
            return []
        size = max(1, min(int(limit), 50))
        body = {
            "size": size,
            "_source": ["title"],
            "query": {"match_phrase_prefix": {"title": prefix}},
        }
        response = self._request(
            "POST",
            f"{self.base_url}/{self.index}/_search",
            json=body,
            headers=self._outgoing_headers(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend suggest failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        hits = response.json().get("hits", {}).get("hits", [])
        seen: set[str] = set()
        out: list[str] = []
        for hit in hits:
            title = hit.get("_source", {}).get("title")
            if isinstance(title, str) and title not in seen:
                seen.add(title)
                out.append(title)
        return out

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        """Aggregations-only search (size=0) — use for the /v1/facets endpoint."""
        payload = self.translate_query(query, size_override=0)
        response = self._request(
            "POST",
            f"{self.base_url}/{self.index}/_search",
            json=payload,
            headers=self._outgoing_headers(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Backend facet lookup failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        return self.extract_facets(response.json())

    def bulk_index(self, docs: list[dict[str, Any]]) -> tuple[int, int]:
        """Index ``docs`` via the ES ``_bulk`` endpoint.

        ``_bulk`` expects alternating action + source NDJSON lines.
        A per-document failure reported in the response is counted in
        ``failed`` without aborting the batch. An HTTP-level error on
        the whole request surfaces as ``backend_unavailable``.
        """
        if not docs:
            return 0, 0
        lines: list[str] = []
        for doc in docs:
            doc_id = doc.get("id")
            action: dict[str, Any] = {"index": {"_index": self.index}}
            if doc_id:
                action["index"]["_id"] = str(doc_id)
            lines.append(json.dumps(action, separators=(",", ":")))
            lines.append(json.dumps(doc, separators=(",", ":"), default=str))
        body = "\n".join(lines) + "\n"
        response = self._request(
            "POST",
            f"{self.base_url}/_bulk",
            content=body.encode("utf-8"),
            headers={**self._outgoing_headers(), "Content-Type": "application/x-ndjson"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AppError(
                "backend_unavailable",
                "Bulk index failed",
                {"reason": str(exc), "status_code": response.status_code},
                503,
            ) from exc
        payload = response.json() if response.content else {}
        items = payload.get("items", []) if isinstance(payload, dict) else []
        ingested = 0
        failed = 0
        for item in items:
            if not isinstance(item, dict):
                failed += 1
                continue
            op = item.get("index") or item.get("create") or {}
            status = int(op.get("status", 0))
            if 200 <= status < 300:
                ingested += 1
            else:
                failed += 1
        # Fallback for adapters / fixtures that do not report per-item
        # status: count every doc as ingested.
        if not items:
            ingested = len(docs)
        return ingested, failed
