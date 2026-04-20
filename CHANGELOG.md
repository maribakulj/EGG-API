# Changelog

All notable changes to EGG-API are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Observability** — Prometheus metrics on `GET /metrics`
  (`egg_requests_total`, `egg_request_duration_seconds`,
  `egg_backend_errors_total`, `egg_rate_limit_hits_total`). Structured
  JSON logs via `structlog`, with `request_id` / `key_id` / `latency_ms`
  bound per request.
- **Caching** — `Cache-Control` + strong `ETag` on `/v1/search`,
  `/v1/records/{id}`, `/v1/facets` with `If-None-Match` → `304` fast path
  (configurable via `CacheConfig`).
- **Security** — CORS middleware driven by `CorsConfig`; security-headers
  middleware sets `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, CSP on `/admin`, and HSTS in production.
- **Security** — Admin login brute-force guard (dedicated
  `InMemoryRateLimiter` keyed by client IP, 10 attempts / 5 min by default).
- **Security** — Admin UI sessions gain an `expires_at` column (TTL 12 h
  by default, configurable); expired tokens are rejected and purged on
  read.
- **Optional endpoints** — `GET /v1/collections` and `GET /v1/schema`
  (SPECS §12.1 / §12.4). `GET /v1/suggest` and `GET /v1/manifest/{id}`
  (§12.2 / §12.3) are declared and return `501 not_implemented` until
  backend plumbing lands.
- **Admin API** — `GET /admin/v1/usage` paginated listing
  (`limit` + `offset`, validated via FastAPI `Query`).
- **Backend** — Bounded retries with exponential backoff in the
  Elasticsearch adapter; typed `backend_unavailable` error on exhaustion.
  ES minor-version gate rejects versions older than 7.

### Changed

- **Security** — Bootstrap admin key is now generated on first run in
  development (stored in a 0600 sidecar file) and required via env var or
  config in production. The legacy `"admin-change-me"` default is refused
  and auto-regenerated.
- **Security** — Admin session cookie defaults to `Secure=True` +
  `SameSite=strict`; both flags are configurable via `AuthConfig`.
- **Security** — `usage_audit_middleware` resolves the raw API key to its
  `key_id` label and never persists or logs the secret.
- **Security** — `ConfigManager.save()` strips `auth.bootstrap_admin_key`
  before writing YAML so secrets cannot leak through config backups.
- **Security** — Admin UI pages are rendered through Jinja2 templates with
  explicit autoescape instead of f-string HTML concatenation.
- **Backend** — `/v1/search` now issues a single backend call, deriving
  facet counts from the same payload (previous implementation made a
  second request).
- **Query policy** — `page` and `page_size` parse errors surface as
  `invalid_parameter` (400). `_parse_bool` strips whitespace and accepts
  `on`/`off` synonyms.
- **Query policy** — `QueryPolicyEngine.compute_cache_key` was migrated to
  a stable JSON + SHA-256 hash (previous implementation used a Pydantic v2
  kwarg that did not exist).
- **Validation** — `AppConfig` gains cross-field validators:
  `security_profile`, `auth.public_mode`, `cors.mode`,
  `allowed_include_fields` coverage, and required/recommended mapping rules
  must declare a source.
- **Mapper** — `date_parser` and `url_passthrough` are defensive: invalid
  values return `None` and are logged; URL passthrough validates scheme and
  netloc.
- **Mapper** — `raw_fields` exposure strips any backend-internal key
  prefixed with `_`.
- **Adapter** — `ElasticsearchAdapter.translate_query` honours the active
  profile's `max_buckets_per_facet` (no more hardcoded 20).
- **Backend** — `httpx.Client` is instantiated with
  `follow_redirects=False`.
- **Storage** — Hot-path indexes on `api_keys(key_hash)`,
  `usage_events(timestamp DESC)`, `usage_events(subject)`,
  `usage_events(status_code)`, `quota_counters(subject)`,
  `ui_sessions(expires_at)`.
- **Admin UI** — Key labels must match `^[a-zA-Z0-9_.-]{1,64}$`.

### Fixed

- `QueryPolicyEngine.compute_cache_key` no longer raises on every call
  (regression introduced with Pydantic v2).

### Internal

- `Container.reload()` is now serialized by a `threading.RLock` to avoid
  races between concurrent config updates.
- Tests: six dedicated suites covering every change landed above
  (`tests/security/test_vague1_hardening.py` through
  `tests/security/test_vague5_spec.py`), plus gap tests for CORS and
  empty-result handling.
