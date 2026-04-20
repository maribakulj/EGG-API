"""HTTP caching helpers for public GET endpoints.

Implements ``Cache-Control`` (public, max-age) and strong ``ETag`` validation
with ``If-None-Match`` support returning ``304 Not Modified``. The TTL is driven
by ``CacheConfig.public_max_age_seconds``; when ``CacheConfig.enabled`` is
``False`` the helper is a no-op.
"""

from __future__ import annotations

from fastapi import Request, Response

from app.dependencies import container


def _etag_matches(header_value: str, etag: str) -> bool:
    candidates = [c.strip() for c in header_value.split(",") if c.strip()]
    return any(c == etag or c == "*" for c in candidates)


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
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    response.headers["ETag"] = etag
    response.headers["Vary"] = "x-api-key"

    inm = request.headers.get("if-none-match")
    if inm and _etag_matches(inm, etag):
        not_modified = Response(status_code=304)
        not_modified.headers["Cache-Control"] = response.headers["Cache-Control"]
        not_modified.headers["ETag"] = etag
        not_modified.headers["Vary"] = "x-api-key"
        return not_modified
    return None
