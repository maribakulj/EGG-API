"""Regression tests for Sprint 3 concurrency + retry hardening (S3.3-S3.8)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.adapters.elasticsearch.adapter import ElasticsearchAdapter
from app.dependencies import container
from app.errors import AppError
from app.schemas.query import NormalizedQuery
from app.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# S3.5 — retry backoff: jitter, cap, deadline
# ---------------------------------------------------------------------------


def test_s3_5_backoff_is_capped() -> None:
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        max_retries=10,
        retry_backoff_seconds=1.0,
        retry_backoff_cap_seconds=0.5,
    )
    # Nominal would be 1 * 2**8 = 256 s; cap keeps it ≤ 0.5 + 25% jitter.
    for attempt in range(10):
        sleep = adapter._compute_sleep(attempt)
        assert sleep <= 0.5 * 1.25 + 1e-9, f"attempt {attempt} -> {sleep}"


def test_s3_5_backoff_has_jitter() -> None:
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        retry_backoff_seconds=0.2,
        retry_backoff_cap_seconds=5.0,
    )
    samples = {adapter._compute_sleep(2) for _ in range(50)}
    # With 50 draws from a continuous uniform jitter we should see multiple
    # distinct values (probability of all-equal is ~0).
    assert len(samples) > 1


def test_s3_5_deadline_short_circuits_retries() -> None:
    class _AlwaysFails(httpx.BaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            raise httpx.ConnectError("down")

    transport = _AlwaysFails()
    client = httpx.Client(transport=transport)
    adapter = ElasticsearchAdapter(
        "http://es.local",
        "records",
        client=client,
        max_retries=100,
        retry_backoff_seconds=0.05,
        retry_backoff_cap_seconds=0.2,
        retry_deadline_seconds=0.1,
    )
    started = time.monotonic()
    with pytest.raises(AppError):
        adapter.search(NormalizedQuery(q="x"))
    elapsed = time.monotonic() - started
    # Deadline 0.1 s + one final backoff slot; must not have hit all 100
    # attempts.
    assert elapsed < 2.0
    assert transport.calls < 100


# ---------------------------------------------------------------------------
# S3.6 — thread-local connection pool
# ---------------------------------------------------------------------------


def test_s3_6_connection_is_reused_within_a_thread(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pool.sqlite3")
    store.initialize()
    conn_a = store._connect()
    conn_b = store._connect()
    assert conn_a is conn_b


def test_s3_6_connection_is_per_thread(tmp_path) -> None:
    import threading

    store = SQLiteStore(tmp_path / "pool.sqlite3")
    store.initialize()
    main_conn = store._connect()
    seen: list[object] = []

    def _grab() -> None:
        seen.append(store._connect())

    t = threading.Thread(target=_grab)
    t.start()
    t.join()
    assert len(seen) == 1
    assert seen[0] is not main_conn


def test_s3_6_close_drops_current_thread_connection(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pool.sqlite3")
    store.initialize()
    first = store._connect()
    store.close()
    second = store._connect()
    assert first is not second


def test_s3_6_connection_reopens_when_db_path_changes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "a.sqlite3")
    store.initialize()
    first = store._connect()
    # Simulate a reconfigure that re-points at a new file (tests do this via
    # Container.reload()).
    store.db_path = tmp_path / "b.sqlite3"
    second = store._connect()
    assert first is not second


# ---------------------------------------------------------------------------
# S3.3 + S3.4 — audit middleware runs SQLite off the event loop
# ---------------------------------------------------------------------------


def test_s3_4_usage_event_recorded_with_threadpool(client) -> None:
    before = container.store.count_usage_events()
    response = client.get("/v1/livez")
    assert response.status_code == 200
    after = container.store.count_usage_events()
    assert after == before + 1


# ---------------------------------------------------------------------------
# S3.7 + S3.8 — concurrency smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_8_event_loop_stays_responsive_under_load() -> None:
    """Ensure the audit middleware does not starve the event loop.

    We hammer the API with 50 concurrent requests via httpx.AsyncClient
    while a sentinel coroutine sleeps in 5 ms slices and counts ticks.
    If the event loop were blocked per-request for the whole SQLite call,
    the sentinel would tick far fewer times than expected.
    """
    from app.main import app

    async def _ticker(stop_event: asyncio.Event) -> int:
        ticks = 0
        while not stop_event.is_set():
            await asyncio.sleep(0.005)
            ticks += 1
        return ticks

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        stop = asyncio.Event()
        ticker = asyncio.create_task(_ticker(stop))
        responses = await asyncio.gather(*[ac.get("/v1/livez") for _ in range(50)])
        stop.set()
        ticks = await ticker

    assert all(r.status_code == 200 for r in responses)
    # The ticker sleeps 5 ms between increments. On a healthy event loop
    # 50 concurrent /v1/livez calls should complete in ≤ a few hundred
    # milliseconds, so the ticker has plenty of room to fire >= 20 times.
    # A middleware that blocks the loop for ~15 ms/request would produce
    # 50*15 = 750 ms of starvation and drop the tick count well below
    # that floor — the previous >= 3 threshold was too lax to catch
    # real regressions.
    assert ticks >= 20, f"ticker fired only {ticks} times — loop looks blocked"


@pytest.mark.asyncio
async def test_s3_8_concurrent_search_does_not_deadlock() -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        started = time.monotonic()
        responses = await asyncio.gather(*[ac.get(f"/v1/search?q=term{i}") for i in range(30)])
        elapsed = time.monotonic() - started

    assert all(r.status_code == 200 for r in responses)
    # Thirty requests against a FakeAdapter should finish comfortably under
    # 5 s even on a constrained CI runner. Any deadlock or event-loop block
    # would push this far higher.
    assert elapsed < 5.0
