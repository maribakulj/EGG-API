"""Regression tests for Vague 2 (H1-H11): backend robustness & perf."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.config.models import AppConfig
from app.dependencies import container
from app.rate_limit.limiter import InMemoryRateLimiter
from app.schemas.query import NormalizedQuery


# ---------------------------------------------------------------------------
# H1 — /search must not issue a second backend call for facets
# ---------------------------------------------------------------------------

class CountingAdapter:
    """Drop-in replacement for FakeAdapter that counts backend calls."""

    def __init__(self) -> None:
        self.search_calls = 0
        self.get_facets_calls = 0

    def detect(self) -> dict[str, Any]:
        return {"detected": True, "version": {"number": "8.0.0"}}

    def health(self) -> dict[str, Any]:
        return {"status": "green"}

    def search(self, query: NormalizedQuery) -> dict[str, Any]:
        self.search_calls += 1
        return {
            "hits": {
                "total": {"value": 2},
                "hits": [
                    {"_source": {"id": "1", "type": "object", "title": "A"}},
                    {"_source": {"id": "2", "type": "object", "title": "B"}},
                ],
            },
            "aggregations": {
                "type": {"buckets": [{"key": "object", "doc_count": 2}]},
            },
        }

    def get_facets(self, query: NormalizedQuery) -> dict[str, dict[str, int]]:
        self.get_facets_calls += 1
        return {"type": {"object": 2}}

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        return {"id": record_id, "type": "object", "title": "x"}

    @staticmethod
    def extract_facets(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        aggs = payload.get("aggregations", {}) or {}
        return {k: {b["key"]: b["doc_count"] for b in v.get("buckets", [])} for k, v in aggs.items()}


def test_h1_search_with_facets_issues_single_backend_call(client) -> None:
    counter = CountingAdapter()
    container.adapter = counter

    response = client.get("/v1/search?q=abc&facet=type")
    assert response.status_code == 200
    payload = response.json()
    assert payload["facets"] == {"type": {"object": 2}}
    assert counter.search_calls == 1
    assert counter.get_facets_calls == 0


def test_h1_search_without_facets_omits_aggregations(client) -> None:
    counter = CountingAdapter()
    container.adapter = counter

    response = client.get("/v1/search?q=abc")
    assert response.status_code == 200
    assert response.json()["facets"] == {}
    assert counter.search_calls == 1


# ---------------------------------------------------------------------------
# H2 — Retry + typed backend errors
# ---------------------------------------------------------------------------

class _FlakyTransport(httpx.BaseTransport):
    def __init__(self, fail_times: int, exc: BaseException) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})


def test_h2_timeout_is_retried_then_succeeds() -> None:
    transport = _FlakyTransport(fail_times=2, exc=httpx.TimeoutException("slow"))
    client = httpx.Client(transport=transport)
    adapter = ElasticsearchAdapter(
        "http://es.local", "records", client=client,
        max_retries=2, retry_backoff_seconds=0,
    )
    nq = NormalizedQuery(q="x", page=1, page_size=10)
    payload = adapter.search(nq)
    assert payload["hits"]["total"]["value"] == 0
    assert transport.calls == 3  # 2 failures + 1 success


def test_h2_timeout_exhausts_retries_raises_backend_unavailable() -> None:
    transport = _FlakyTransport(fail_times=99, exc=httpx.ConnectError("down"))
    client = httpx.Client(transport=transport)
    adapter = ElasticsearchAdapter(
        "http://es.local", "records", client=client,
        max_retries=1, retry_backoff_seconds=0,
    )
    from app.errors import AppError

    nq = NormalizedQuery(q="x", page=1, page_size=10)
    with pytest.raises(AppError) as excinfo:
        adapter.search(nq)
    assert excinfo.value.code == "backend_unavailable"
    assert excinfo.value.status_code == 503
    assert transport.calls == 2  # initial + 1 retry


def test_h2_5xx_response_is_retried() -> None:
    sequence = [500, 502, 200]
    call_log = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = call_log["n"]
        call_log["n"] += 1
        status = sequence[idx]
        body = {"hits": {"total": {"value": 0}, "hits": []}} if status == 200 else {"error": "boom"}
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    adapter = ElasticsearchAdapter(
        "http://es.local", "records", client=client,
        max_retries=2, retry_backoff_seconds=0,
    )
    adapter.search(NormalizedQuery(q="x"))
    assert call_log["n"] == 3


# ---------------------------------------------------------------------------
# H3 — max_depth boundary semantics
# ---------------------------------------------------------------------------

def test_h3_requested_depth_at_boundary_is_allowed(client) -> None:
    cfg = container.config_manager.config
    cfg.profiles[cfg.security_profile].max_depth = 100
    cfg.profiles[cfg.security_profile].page_size_max = 50
    # page*page_size == max_depth (100) must pass
    response = client.get("/v1/search?q=x&page=5&page_size=20")
    assert response.status_code == 200


def test_h3_requested_depth_over_boundary_is_rejected(client) -> None:
    cfg = container.config_manager.config
    cfg.profiles[cfg.security_profile].max_depth = 100
    response = client.get("/v1/search?q=x&page=6&page_size=20")
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "unsupported_operation"
    assert body["error"]["details"]["requested"] == 120
    assert body["error"]["details"]["max_depth"] == 100


def test_h3_non_integer_page_returns_invalid_parameter(client) -> None:
    response = client.get("/v1/search?q=x&page=abc")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_h3_boolean_parser_strips_whitespace_and_case(client) -> None:
    for value in ("TRUE", "  yes ", "On", "FALSE", "no"):
        response = client.get(f"/v1/search?q=x&has_digital={value}")
        assert response.status_code == 200, f"failed for {value!r}"


# ---------------------------------------------------------------------------
# H4 — Cache-Control + ETag + 304
# ---------------------------------------------------------------------------

def test_h4_search_emits_cache_control_and_etag(client) -> None:
    response = client.get("/v1/search?q=abc")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control", "").startswith("public, max-age=")
    etag = response.headers.get("ETag")
    assert etag and etag.startswith('"search:')


def test_h4_if_none_match_returns_304(client) -> None:
    response = client.get("/v1/search?q=abc")
    etag = response.headers["ETag"]
    cached = client.get("/v1/search?q=abc", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.headers["ETag"] == etag
    assert not cached.content


def test_h4_records_emit_etag(client) -> None:
    response = client.get("/v1/records/abc")
    assert response.status_code == 200
    assert response.headers.get("ETag") == '"record:abc"'


def test_h4_facets_emit_etag(client) -> None:
    response = client.get("/v1/facets?q=abc&facet=type")
    assert response.status_code == 200
    assert response.headers.get("ETag", "").startswith('"facets:')


def test_h4_cache_disabled_omits_headers(client) -> None:
    container.config_manager.config.cache.enabled = False
    try:
        response = client.get("/v1/search?q=abc")
        assert response.status_code == 200
        assert "ETag" not in response.headers
        assert "Cache-Control" not in response.headers
    finally:
        container.config_manager.config.cache.enabled = True


# ---------------------------------------------------------------------------
# H5 — Admin login anti-brute-force
# ---------------------------------------------------------------------------

def test_h5_login_rate_limit_returns_429(client) -> None:
    container.login_rate_limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60)

    r1 = client.post("/admin/login", data={"api_key": "bad"}, follow_redirects=False)
    r2 = client.post("/admin/login", data={"api_key": "bad"}, follow_redirects=False)
    r3 = client.post("/admin/login", data={"api_key": "bad"}, follow_redirects=False)

    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 429
    assert "Too many attempts" in r3.text


def test_h5_rate_limit_runs_before_credential_check(client, admin_headers) -> None:
    container.login_rate_limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60)
    bad = client.post("/admin/login", data={"api_key": "bad"}, follow_redirects=False)
    assert bad.status_code == 401
    # Good credential arrives second: still rate-limited.
    good = client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    assert good.status_code == 429


# ---------------------------------------------------------------------------
# H11 — follow_redirects disabled on the backend client
# ---------------------------------------------------------------------------

def test_h11_backend_client_does_not_follow_redirects() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records")
    assert adapter.client.follow_redirects is False


def test_h11_redirect_is_not_followed_by_adapter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "es.local":
            return httpx.Response(302, headers={"location": "http://evil.example/"})
        # If the adapter DID follow, we'd see a call here.
        raise AssertionError("adapter followed redirect to unexpected host")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=False)
    adapter = ElasticsearchAdapter("http://es.local", "records", client=client)

    # The 302 surfaces as a non-2xx: get_record short-circuits 404, but 302
    # passes through raise_for_status as an error — wrapped as backend_unavailable.
    from app.errors import AppError

    with pytest.raises(AppError) as excinfo:
        adapter.get_record("42")
    assert excinfo.value.code == "backend_unavailable"


# ---------------------------------------------------------------------------
# Adapter helpers
# ---------------------------------------------------------------------------

def test_translate_query_respects_max_buckets_per_facet() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records")
    nq = NormalizedQuery(q="x", facets=["type"])
    body = adapter.translate_query(nq, max_buckets_per_facet=5)
    assert body["aggs"]["type"]["terms"]["size"] == 5


def test_translate_query_size_override_zero_for_facets_only() -> None:
    adapter = ElasticsearchAdapter("http://es.local", "records")
    nq = NormalizedQuery(q="x", page=1, page_size=20, facets=["type"])
    body = adapter.translate_query(nq, size_override=0)
    assert body["size"] == 0
    assert "aggs" in body


def test_extract_facets_tolerates_missing_aggregations() -> None:
    assert ElasticsearchAdapter.extract_facets({}) == {}
    assert ElasticsearchAdapter.extract_facets({"aggregations": None}) == {}


# ---------------------------------------------------------------------------
# Compute cache key regression (previously broken due to Pydantic v2)
# ---------------------------------------------------------------------------

def test_policy_cache_key_is_deterministic() -> None:
    cfg = AppConfig()
    from app.query_policy.engine import QueryPolicyEngine

    engine = QueryPolicyEngine(cfg)
    nq1 = NormalizedQuery(q="a", filters={"type": ["x", "y"]})
    nq2 = NormalizedQuery(q="a", filters={"type": ["x", "y"]})
    assert engine.compute_cache_key(nq1) == engine.compute_cache_key(nq2)
    assert engine.compute_cache_key(nq1) != engine.compute_cache_key(
        NormalizedQuery(q="a", filters={"type": ["y", "x"]})
    )
