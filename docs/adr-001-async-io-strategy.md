# ADR 001 — Async I/O strategy for EGG-API storage

Status: **Accepted** (2026-04, Sprint 3)
Supersedes: n/a

## Context

Sprint 3 of the hardening plan (see SPECS / audit analysis) called out that
SQLite I/O ran on the asyncio event loop in two places:

1. `usage_audit_middleware` — declared `async def`, calls
   `SQLiteStore.log_usage_event()` and `ApiKeyManager.get_identity()`
   directly on every request.
2. The public/admin routes — declared plain `def`, so FastAPI already
   scheduled them in its threadpool via anyio. These were **not** blocking
   the event loop.

The `httpx.Client` retry loop in `ElasticsearchAdapter._request` also used
`time.sleep()`, but because it is only reached from sync routes it runs in
the same threadpool, not on the loop.

Two options were on the table:

- **Option A — threadpool wrappers** (`starlette.concurrency.run_in_threadpool`
  around the middleware's sync calls, keep `sqlite3` as-is).
- **Option B — `aiosqlite`** everywhere, make every route `async def`, pool
  via a single shared aiosqlite connection.

## Decision

Go with **Option A** for v0.3.x.

## Rationale

- **Scope fit**: EGG-API is an MVP facade. SQLite is used for state
  (~10k rows at realistic scale) — not the hot path. The hot path is
  Elasticsearch over `httpx`, which is already threadpool-friendly.
- **Blast radius**: Option B rewires every route into `async def`, which
  cascades into every test and fixture. A typed-contract shift like that
  would dominate a two-week sprint and leave no time for the other items
  on the Sprint 3 backlog (retry backoff, connection pooling, concurrency
  tests).
- **Performance envelope**: with WAL journaling and a thread-local
  connection pool (this sprint), sync SQLite on a threadpool handles
  several thousand RPS on a single worker. We are nowhere near that
  ceiling.
- **Reversibility**: a future Sprint can revisit B when the state store
  outgrows SQLite (Postgres, MySQL, ClickHouse for `usage_events`). At
  that point `async` is the natural fit and Option A's adapters come off.

## Implementation notes (this sprint)

- `SQLiteStore` now keeps one `sqlite3.Connection` per OS thread, keyed
  by `db_path`. `check_same_thread=False` on the instance is safe because
  the thread-local pool guarantees no cross-thread access.
- `usage_audit_middleware` wraps both `get_identity` and `log_usage_event`
  in `run_in_threadpool`. Storage failures are caught and logged — they
  must not mask the original response/exception.
- `ElasticsearchAdapter` retries now use `random.uniform(-25%, +25%)`
  jitter around the capped exponential backoff, and a global
  `retry_deadline_seconds` (default 30 s) prevents retries from
  compounding past the incoming request's own timeout.
- Three new config knobs (opt-in, sane defaults):
  - `backend.retry_backoff_cap_seconds` (5 s)
  - `backend.retry_deadline_seconds` (30 s)

## Consequences

Positive:
- No route signatures change; existing tests and third-party code keep
  working.
- Event-loop blocking on SQLite is gone. An event-loop-sensitive health
  check (e.g. an `asyncio.sleep(0)` probe) stays responsive under load.
- Thread-local connections remove the per-request `sqlite3.connect()`
  ~100 µs tax and reduce WAL contention.

Negative:
- Still bottlenecked by the Starlette threadpool size (default 40). At
  very high RPS the limit is concurrent threads, not the event loop. A
  Redis-backed rate-limit store or Postgres migration would raise that
  ceiling.
- `sqlite3` without `async` means we cannot easily cancel in-flight
  queries when an HTTP client disconnects. Acceptable for admin/audit
  workloads; revisit with Option B if needed.

## References

- Audit report §1.7, §3.3
- Sprint 3 plan in this repository's CHANGELOG
- Starlette docs on `run_in_threadpool`
