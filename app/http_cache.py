"""HTTP caching helpers for public GET endpoints.

Implements ``Cache-Control`` and **weak** ``ETag`` validation with
``If-None-Match`` returning ``304 Not Modified``. The TTL is driven by
``CacheConfig.public_max_age_seconds``; when ``CacheConfig.enabled`` is
``False`` the helper is a no-op.

The ETags are weak (``W/"…"`` per RFC 7232) because they're derived from
the *request shape* (normalized query key, record id) rather than a hash
of the response body. That gives semantic equivalence across equivalent
requests without committing to byte-level identity — which would require
hashing the rendered body and defeat the fast-path win of 304 responses
for unchanged queries. If the backend index changes between two calls
with the same query, the ETag stays the same within a single cache TTL
window; operators who need stricter freshness should lower
``CacheConfig.public_max_age_seconds`` or disable caching entirely.

The ``Cache-Control`` directive follows the auth mode:

- ``anonymous_allowed`` → ``public, max-age=N`` (safe for shared caches).
- ``api_key_optional``/``api_key_required`` → ``private, max-age=N``. The
  response is keyed to a caller holding a specific API key; shared caches
  MUST NOT store it. Dropping the old ``Vary: x-api-key`` in favor of
  ``private`` is both more correct (intermediaries ignore ``Vary`` on secret
  headers inconsistently) and keeps the browser cache tight to the key
  that fetched the response.
"""

from __future__ import annotations

from fastapi import Request, Response

from app.dependencies import container


def _strip_weak_prefix(tag: str) -> str:
    """Strip the ``W/`` prefix for weak ETag comparison (RFC 7232 §2.3.2)."""
    return tag[2:] if tag.startswith("W/") else tag


def _etag_matches(header_value: str, etag: str) -> bool:
    """Weak-comparison match against an ``If-None-Match`` header value.

    Per RFC 7232 §3.2 the weak comparison ignores the ``W/`` prefix, so
    ``W/"x"`` and ``"x"`` are equivalent. We normalize both sides before
    comparing; this also tolerates intermediaries that strip or add the
    prefix.
    """
    target = _strip_weak_prefix(etag)
    candidates = [c.strip() for c in header_value.split(",") if c.strip()]
    return any(c == "*" or _strip_weak_prefix(c) == target for c in candidates)


def _cache_control_directive(max_age: int) -> str:
    mode = container.config_manager.config.auth.public_mode
    directive = "public" if mode == "anonymous_allowed" else "private"
    return f"{directive}, max-age={max_age}"


def apply_cache_headers(
    request: Request,
    response: Response,
    etag: str,
) -> Response | None:
    """Set Cache-Control + ETag; return a 304 Response if the client has it.

    Returns ``None`` when the response should be built normally. Callers must
    still produce the body in the non-304 branch.
    """
    cache_cfg = container.config_manager.config.cache
    if not cache_cfg.enabled:
        return None

    max_age = max(0, int(cache_cfg.public_max_age_seconds))
    cache_control = _cache_control_directive(max_age)
    response.headers["Cache-Control"] = cache_control
    response.headers["ETag"] = etag

    inm = request.headers.get("if-none-match")
    if inm and _etag_matches(inm, etag):
        not_modified = Response(status_code=304)
        not_modified.headers["Cache-Control"] = cache_control
        not_modified.headers["ETag"] = etag
        return not_modified
    return None
