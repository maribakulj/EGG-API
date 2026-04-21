"""Regression tests for Sprint 8 feature additions (S8.1 - S8.5)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.elasticsearch.adapter import (
    ElasticsearchAdapter,
    _decode_cursor,
    _encode_cursor,
)
from app.dependencies import container
from app.errors import AppError
from app.rate_limit.redis_limiter import RedisRateLimiter, build_rate_limiter
from app.schemas.query import NormalizedQuery

# ---------------------------------------------------------------------------
# S8.2 — /v1/auth/whoami
# ---------------------------------------------------------------------------


def test_s8_2_whoami_anonymous(client) -> None:
    response = client.get("/v1/auth/whoami")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["subject"].startswith("ip:")


def test_s8_2_whoami_authenticated(client, admin_headers) -> None:
    response = client.get("/v1/auth/whoami", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["key_id"] == "admin"
    assert body["status"] == "active"
    # Raw secret must never echo back.
    assert body.get("key") is None
    assert admin_headers["x-api-key"] not in json.dumps(body)


def test_s8_2_whoami_invalid_key_is_anonymous(client) -> None:
    response = client.get("/v1/auth/whoami", headers={"x-api-key": "not-a-real-key"})
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


# ---------------------------------------------------------------------------
# S8.1 — cursor pagination
# ---------------------------------------------------------------------------


def test_s8_1_encode_decode_roundtrip() -> None:
    cursor = _encode_cursor(["abc", 42])
    assert _decode_cursor(cursor) == ["abc", 42]


def test_s8_1_decode_malformed_raises_invalid_parameter() -> None:
    with pytest.raises(AppError) as exc:
        _decode_cursor("not-base64-$$$")
    assert exc.value.code == "invalid_parameter"


def test_s8_1_cursor_bypasses_max_depth(client) -> None:
    # Profile max_depth=2000 with the default config; page*page_size would
    # exceed it, but the cursor path must still pass.
    cursor = _encode_cursor(["last-id"])
    response = client.get(
        f"/v1/search?q=x&page=999&page_size=50&cursor={cursor}",
    )
    # Rejected without cursor, accepted with cursor.
    assert response.status_code == 200


def test_s8_1_translate_query_emits_search_after_with_cursor() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records")
    cursor = _encode_cursor(["abc"])
    body = adapter.translate_query(NormalizedQuery(q="x", cursor=cursor))
    assert "from" not in body
    assert body["search_after"] == ["abc"]
    assert body["sort"] == [{"_id": "asc"}]


def test_s8_1_translate_query_uses_from_size_without_cursor() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records")
    body = adapter.translate_query(NormalizedQuery(q="x", page=2, page_size=10))
    assert body["from"] == 10
    assert "search_after" not in body


# ---------------------------------------------------------------------------
# S8.3 — /v1/suggest
# ---------------------------------------------------------------------------


def test_s8_3_suggest_returns_list(client) -> None:
    response = client.get("/v1/suggest?q=abc")
    assert response.status_code == 200
    body = response.json()
    assert body["q"] == "abc"
    assert isinstance(body["suggestions"], list)


def test_s8_3_suggest_rejects_empty_q(client) -> None:
    response = client.get("/v1/suggest?q=")
    assert response.status_code == 400


def test_s8_3_suggest_limit_bounds(client) -> None:
    response = client.get("/v1/suggest?q=abc&limit=0")
    assert response.status_code == 400
    response = client.get("/v1/suggest?q=abc&limit=50")
    assert response.status_code == 400


def test_s8_3_elasticsearch_suggest_hits_backend() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {"_source": {"title": "Alpha"}},
                        {"_source": {"title": "Alpha"}},  # dedup in the adapter
                        {"_source": {"title": "Beta"}},
                    ]
                }
            },
        )

    client_ = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=client_,
        max_retries=0,
        retry_backoff_seconds=0,
    )
    result = adapter.suggest("alp", limit=5)
    assert result == ["Alpha", "Beta"]
    assert seen["body"]["query"]["match_phrase_prefix"] == {"title": "alp"}


# ---------------------------------------------------------------------------
# S8.4 — JSON-LD
# ---------------------------------------------------------------------------


def test_s8_4_records_accept_jsonld(client) -> None:
    response = client.get(
        "/v1/records/abc",
        headers={"accept": "application/ld+json"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")
    body = response.json()
    assert body["@context"]["title"] == "name"
    assert body["id"] == "abc"


def test_s8_4_search_format_jsonld(client) -> None:
    response = client.get("/v1/search?q=abc&format=jsonld")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")
    body = response.json()
    assert body["@type"] == "ItemList"
    assert "results" in body
    assert "@context" in body


def test_s8_4_search_invalid_format(client) -> None:
    response = client.get("/v1/search?q=abc&format=xml")
    assert response.status_code == 400


def test_s8_4_accept_only_default_json_still_json(client) -> None:
    response = client.get("/v1/search?q=abc")
    assert response.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# S8.5 — Redis rate limiter (opt-in)
# ---------------------------------------------------------------------------


def test_s8_5_build_rate_limiter_falls_back_without_env(monkeypatch) -> None:
    monkeypatch.delenv("EGG_RATE_LIMIT_REDIS_URL", raising=False)
    from app.rate_limit.limiter import InMemoryRateLimiter

    limiter = build_rate_limiter(max_requests=5, window_seconds=60)
    assert isinstance(limiter, InMemoryRateLimiter)


def test_s8_5_redis_limiter_allows_until_cap() -> None:
    class _FakePipe:
        def __init__(self, store: dict[str, int]):
            self.store = store
            self._incr_key: str | None = None
            self._incr_amount: int = 0

        def incr(self, key: str, amount: int) -> None:
            self._incr_key = key
            self._incr_amount = amount

        def expire(self, key: str, ttl: int) -> None:
            pass

        def execute(self):
            assert self._incr_key is not None
            self.store[self._incr_key] = self.store.get(self._incr_key, 0) + self._incr_amount
            return [self.store[self._incr_key], True]

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, int] = {}

        def pipeline(self) -> _FakePipe:
            return _FakePipe(self.store)

        def ping(self) -> bool:
            return True

    limiter = RedisRateLimiter(
        redis_client=_FakeRedis(),
        max_requests=2,
        window_seconds=60,
    )
    assert limiter.allow("sub-a") is True
    assert limiter.allow("sub-a") is True
    assert limiter.allow("sub-a") is False
    # Separate subjects share no bucket.
    assert limiter.allow("sub-b") is True


def test_s8_5_redis_limiter_fails_open_on_backend_error() -> None:
    class _BoomRedis:
        def pipeline(self) -> None:
            raise RuntimeError("redis is on fire")

    limiter = RedisRateLimiter(
        redis_client=_BoomRedis(),
        max_requests=1,
        window_seconds=60,
    )
    # Fail-open: Redis outage must not block user traffic.
    assert limiter.allow("subject") is True
    assert limiter.allow("subject") is True


# ---------------------------------------------------------------------------
# Smoke: existing container path still works after Sprint 8 swaps
# ---------------------------------------------------------------------------


def test_sprint_8_container_still_built_correctly() -> None:
    # No env, no Redis -> falls back to InMemoryRateLimiter. Container
    # wiring must survive.
    from app.rate_limit.limiter import InMemoryRateLimiter

    assert isinstance(container.rate_limiter, InMemoryRateLimiter)
    assert isinstance(container.login_rate_limiter, InMemoryRateLimiter)
