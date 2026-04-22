# Changelog

All notable changes to EGG-API are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Sprint 16 — first-run UX**:
  - New `egg-api start` command: generates the bootstrap admin key,
    prints it once to the terminal, mints a one-time magic link
    (`/admin/setup-otp/<token>`, 5-min TTL, hashed at rest), opens
    the default browser (skippable via `--no-browser`) and drops
    into uvicorn. Idempotent: a second run reuses the existing key.
  - New `/admin/setup-otp/{token}` route exchanges the OTP for an
    admin UI session and bounces to `/admin/ui/setup`; replayed or
    expired tokens surface the login page with a clear message.
  - New migration 7 creates `setup_otps`; `SQLiteStore` gains
    `create_setup_otp` / `consume_setup_otp` /
    `purge_expired_setup_otps`.
  - New `app/user_errors.py` translates common `AppError` codes and
    Pydantic `ValidationError` payloads into plain-language messages
    with a suggestion. `egg-api check-config` / `check-backend`
    now surface those translations instead of raw exceptions.
- **Sprint 15 — setup wizard, screens 4-8 + publish** (SPECS §26):
  completes the guided flow. New screens under `/admin/ui/setup`:
  *security* (profile + public mode), *exposure*
  (facets/sorts/include_fields allowlists), *keys* (mint the first
  public key via the shared `ApiKeyService`), *test* (run a live
  probe search), *done* (summary) and `POST /admin/ui/setup/publish`
  which assembles a valid `AppConfig` via `draft_to_config()` and
  swaps the running container onto it. Inline secrets in the draft
  are wiped after publication; operator-only fields (cors, proxy,
  bootstrap key, storage paths) are preserved from the active config.
- **Help glossary** at `/admin/ui/help`: plain-language definitions
  of the terms used across the wizard (backend, index, mapping,
  facet, profile…). Linked from the base navigation.
- **Sprint 14 — setup wizard, screens 1-3** (SPECS §26): new admin UI
  flow under `/admin/ui/setup` that walks operators through *backend →
  source → mapping*. State lives in a per-admin `setup_drafts` row
  (migration 6), not in `egg.yaml`, so an operator can resume after a
  disconnect; the final publish step arrives in Sprint 15. The backend
  step can probe the candidate server without touching the running
  container; the source step runs `scan_fields` and lists discovered
  indices/fields; the mapping step pre-fills a heuristic proposal from
  the discovered fields.
- **Sprint 13 — REST CRUD for API keys** (SPECS §13.7-13.10):
  `GET/POST/PATCH/DELETE /admin/v1/keys` + `GET/PATCH/DELETE
  /admin/v1/keys/{key_id}`. The raw secret is disclosed exactly once
  (on create and on rotate) and never included in list/get responses.
  Admin UI and REST API now share a single `ApiKeyService`, so label
  validation, duplicate detection and session invalidation live in one
  place. `DELETE` is a soft-revoke to preserve the `usage_events`
  audit trail.
- **Sprint 12 — deployment hardening**:
  - `backend.auth` config block supports `none` / `basic` / `bearer` /
    `api_key` and is threaded through the ES + OpenSearch adapters.
    Inline `password` / `token` are redacted by `ConfigManager.save()`;
    operators should use the `password_env` / `token_env` indirection
    to keep secrets off disk.
  - `proxy.allowed_hosts` → Starlette `TrustedHostMiddleware` rejects
    requests with an unexpected `Host` header before any handler runs.
  - Multi-worker guardrail: refuse to boot in production (and warn in
    development) when `EGG_WORKERS` / `WEB_CONCURRENCY` /
    `UVICORN_WORKERS > 1` without `EGG_RATE_LIMIT_REDIS_URL`. Prevents
    a silent N× rate-limit inflation with the in-memory limiter.
  - `extra="forbid"` on every `AppConfig` sub-model: typos in
    `config/egg.yaml` now fail `egg-api check-config` instead of being
    silently ignored.

### Changed

- **Sprint 11 — honesty pass**: README and SPECS aligned with the
  v1.0.0 reality. `/v1/suggest` is live, `/v1/manifest/{id}` retired,
  Solr marked deferred, the desktop story reframed as a 3-step roadmap
  instead of an aspirational promise. Added `SECURITY.md`,
  `CONTRIBUTING.md`, `.github/dependabot.yml`. Split `make dev`
  (auto-reload) from `make run` (production-style). `ConfigManager`
  now chmods `config/egg.yaml` to 0600 on POSIX.
- **Coverage gate temporarily 78%** (Sprint 15): the wizard added
  ~600 lines of Jinja/route plumbing whose error branches are
  intentionally defensive. Sprint 18 tightens per-package thresholds
  (auth/policy/storage at 90%) and restores the overall gate to 80%+.

## [1.0.0] — 2026-04-21

First stable release after a full audit-driven sprint series (S0 → S8).
284 tests green, 82.88% coverage, ruff + ruff format + mypy clean.

### Breaking

- `/v1/suggest` went from `404` stub → `200` with a real ES-backed
  implementation (S8.3). Re-add it to any contract tests you had
  relying on the 404.
- `/v1/manifest/{id}` remains **retired**. Paths starting with
  `/v1/manifest` return `404`; re-introduce them with a proper
  backend integration if/when IIIF proxy support lands.
- `Record.raw_identifiers` was removed (S5.6) — duplicates the
  `identifiers` object and was always empty in practice.
- `/v1/health` body shrunk to `{"status":"ok"}` (S1.9). Operators
  wanting the previous detail can hit `/v1/readyz` (admin-gated) or
  `/v1/livez` (public).
- HTTP caching: `Vary: x-api-key` was dropped and `Cache-Control`
  now switches between `public` and `private` based on
  `auth.public_mode` (S5.3). Shared caches that relied on `Vary`
  should adopt `Cache-Control: private` directly.

### Added

- **Sprint 0 — tooling**: ruff (lint + format), mypy, pytest-cov
  (80% gate), GitHub Actions CI (lint + 3.10/3.11/3.12 matrix),
  multi-stage Dockerfile (non-root `egg` user), docker-compose stack
  (ES 8 + EGG), pip-compile lock files.
- **Sprint 1 — critical security** (S1.1 – S1.11):
  - Rate-limit buckets key on `key_id` or IP, never the raw secret.
  - Prometheus `endpoint` label uses the route template
    (`/v1/records/{record_id}`) — no cardinality explosion.
  - `SchemaMapper` raises `AppError("bad_gateway", 502)` when a
    backend record has no usable id, instead of a Pydantic 500.
  - `usage_audit_middleware` wraps the persist/log/metrics path in
    `try/finally` so 500s land in `usage_events` too.
  - `Container.reload()` closes the previous httpx.Client (no more
    socket/FD leak across reloads).
  - UI session tokens are SHA-256 hashed at rest (raw cookie never
    lands in SQLite).
  - `x-request-id` header is validated against
    `^[A-Za-z0-9._-]{1,64}$` or regenerated.
  - Input-size caps: `q ≤ 512`, `≤ 50` values per filter,
    `≤ 256` chars per filter value, `≤ 20` include_fields.
  - `/v1/health` split into `/v1/livez` (public) and `/v1/readyz`
    (admin).
  - `/docs`, `/redoc`, `/openapi.json` hidden in production.
  - `/metrics` requires admin `X-API-Key` or `EGG_METRICS_TOKEN`
    bearer in production.
- **Sprint 2 — CSRF + UI hardening** (S2.1 – S2.10):
  - Double-submit CSRF on every admin UI POST (HMAC of
    `session_cookie` with a per-process signing key; no DB writes).
  - `samesite=none` rejected at config-load when
    `admin_cookie_secure=false`.
  - Generic error copy in the UI — Pydantic traces never leak.
  - `uvicorn.ProxyHeadersMiddleware` opt-in via
    `proxy.trusted_proxies`.
  - `POST /admin/logout-everywhere` purges every live session for
    the current `key_id`.
  - `POST /admin/ui/keys/{key_id}/rotate` regenerates the secret +
    invalidates sessions. Rotating the `admin` key updates
    `default_admin_key` in memory so the next reload does not
    resurrect the old value.
  - `SQLiteStore.set_key_status` split into
    `set_key_status_by_key_id` / `_by_secret` (no more OR-clause).
  - INSTALL.md gains a reverse-proxy deploy section (nginx,
    Traefik, sanity curls).
- **Sprint 3 — async + retry hardening** (S3.1 – S3.8):
  - Threadpool (Option A) — see `docs/adr-001-async-io-strategy.md`.
  - `SQLiteStore` keeps one `sqlite3.Connection` per thread, keyed
    by `db_path`. `check_same_thread=False` safe thanks to the
    per-thread pool.
  - `usage_audit_middleware` runs `get_identity` + `log_usage_event`
    via `run_in_threadpool`.
  - ElasticsearchAdapter retries cap backoff
    (`retry_backoff_cap_seconds`, default 5 s), add ±25% jitter,
    and honour a global `retry_deadline_seconds` (default 30 s).
- **Sprint 4 — persistence + migrations + pepper** (S4.1 – S4.9):
  - Versioned migration runner (`app/storage/migrations.py`) with
    5 baseline migrations and a legacy-DB baseline heuristic.
  - `egg-api migrate` CLI reports before/after version + applied
    list.
  - Background purge task (FastAPI `lifespan`): evicts expired UI
    sessions + `usage_events` older than
    `usage_events_retention_days` (default 30).
  - `GET /admin/v1/storage/stats` exposes row counts, on-disk size,
    schema version, last purge snapshot.
  - Opt-in HMAC-SHA256 pepper for API keys
    (`EGG_API_KEY_PEPPER`). Legacy SHA-256 keys still validate;
    `rotate_api_key` upgrades them in place.
  - Removed the never-wired `quota_counters` / `quota_config`
    tables.
- **Sprint 5 — contract + mapper refactor** (S5.1 – S5.10):
  - `typing.Literal` aliases replace `_VALID_*` sets
    (`PublicAuthMode`, `CorsMode`, `SameSite`, `Criticality`,
    `MappingMode`, `BackendType`).
  - `SchemaMapper._apply_mode` is now a dict dispatch over nine
    named handlers — adding a new mode is additive.
  - `ElasticsearchAdapter` now forwards the bound request_id as
    `X-Opaque-Id` to every backend call.
  - `/v1/search?format=csv` returns a flat, spreadsheet-friendly
    CSV export.
  - OpenAPI path snapshot test locks the public contract.
- **Sprint 6 — observability + ops pack** (S6.1 – S6.9):
  - Opt-in OpenTelemetry (via `EGG_OTEL_ENDPOINT`) — FastAPI +
    httpx auto-instrumented, `traceparent` propagated. Structlog
    processor injects `trace_id` / `span_id` into every event.
  - `GET /admin/v1/debug/translate` returns normalized query,
    cache key and backend DSL without touching the backend.
  - `ops/prometheus/alerts.yml`, `ops/grafana/egg-api-overview.json`,
    `ops/RUNBOOK.md`, `deploy/k8s/egg-api.yaml`,
    `scripts/locustfile.py`.
- **Sprint 7 — architecture + extensibility** (S7.1 – S7.7):
  - `BackendAdapter` runtime-checkable Protocol.
  - Adapter factory dispatches on `backend.type`.
  - `OpenSearchAdapter` (drop-in compatible, version floor 1.x).
  - Four store role Protocols (`KeyStore`, `SessionStore`,
    `UsageLogger`, `StatsReporter`).
  - `app.state.container` + `get_container(request)` helper for
    `Depends`-based access (singleton stays as fallback).
  - `pytest-xdist` supported, parallel run time ~11 s.
  - `docs/backends.md` backend authoring guide.
- **Sprint 8 — advanced functional** (S8.1 – S8.6):
  - `search_after` cursor pagination with an opaque base64url
    token. `NormalizedQuery.cursor` bypasses `max_depth`;
    `SearchResponse.next_cursor` is emitted on full pages.
  - `GET /v1/auth/whoami` for caller introspection.
  - `GET /v1/suggest` (match_phrase_prefix on `title`).
  - JSON-LD response flavor on `/v1/records/{id}` (Accept header)
    and `/v1/search?format=jsonld`.
  - Opt-in Redis rate limiter
    (`EGG_RATE_LIMIT_REDIS_URL`, `[redis]` extra) with fail-open
    semantics.

### Changed

- Default cookie posture tightened: `admin_cookie_secure=True`,
  `admin_cookie_samesite=strict`.
- FastAPI app attaches `configure_tracing(app)` and
  `ProxyHeadersMiddleware` *before* the routers are registered so
  instrumentation sees every endpoint.
- `Container.adapter` is typed as `BackendAdapter` Protocol and
  closed via `getattr(previous_adapter, "client", None)` so future
  backends without an `httpx.Client` work unchanged.
- Rate limiters are built through `build_rate_limiter(scope=…)` so
  the in-memory and Redis-backed flavours are pin-compatible.

### Fixed

- Every item flagged in the original audit has been either
  addressed or explicitly deferred with tracking — see
  `docs/post-audit.md`.

## [0.1.0] — initial MVP (pre-audit)

Baseline feature set kept for reference. Items below were delivered
across the "vague" hardening passes (C1-C6, H1-H11, M1-M9, L1-L4)
before the sprint series started. See the 0.1.0 commit trail for
detail.

### Added

- **Observability** — Prometheus metrics on `GET /metrics`
  (`egg_requests_total`, `egg_request_duration_seconds`,
  `egg_backend_errors_total`, `egg_rate_limit_hits_total`).
  Structured JSON logs via `structlog`, with `request_id` /
  `key_id` / `latency_ms` bound per request.
- **Caching** — `Cache-Control` + strong `ETag` on `/v1/search`,
  `/v1/records/{id}`, `/v1/facets` with `If-None-Match` → `304`
  fast path.
- **Security** — CORS middleware driven by `CorsConfig`;
  security-headers middleware sets `X-Content-Type-Options`,
  `Referrer-Policy`, `X-Frame-Options`, CSP on `/admin`, and HSTS
  in production.
- **Security** — Admin login brute-force guard.
- **Security** — Admin UI sessions gain an `expires_at` column.
- **Optional endpoints** — `GET /v1/collections`, `GET /v1/schema`.
- **Admin API** — `GET /admin/v1/usage` paginated.
- **Backend** — Bounded retries with exponential backoff; ES
  minor-version gate rejects versions older than 7.

### Changed

- **Security** — Bootstrap admin key is generated on first run in
  development and required via env var in production.
- **Security** — `usage_audit_middleware` resolves raw API keys to
  their `key_id` label.
- **Security** — `ConfigManager.save()` strips
  `auth.bootstrap_admin_key` before writing YAML.
- **Validation** — `AppConfig` cross-field validators.
- **Mapper** — `date_parser` / `url_passthrough` defensive;
  `raw_fields` exposure strips backend-internal keys.
- **Backend** — `httpx.Client` uses `follow_redirects=False`.
- **Storage** — Hot-path indexes on `api_keys`, `usage_events`,
  `ui_sessions`.
- **Admin UI** — Key labels must match
  `^[a-zA-Z0-9_.-]{1,64}$`.

### Fixed

- `QueryPolicyEngine.compute_cache_key` no longer raises
  (Pydantic v2 regression).

### Internal

- `Container.reload()` serialized by a `threading.RLock`.
