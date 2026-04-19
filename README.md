# PISCO-API

**Safe, normalized, backend-agnostic public API in front of a GLAM search backend.**

PISCO-API is a FastAPI service that sits between a public consumer (catalog, portal, third-party client) and your existing search backend (Elasticsearch today, OpenSearch / Solr planned). It does three jobs:

1. **Normalize** — turn heterogeneous backend records into a stable public `Record` schema via a configuration-driven mapper.
2. **Protect** — never expose the raw backend DSL, enforce a configurable `SecurityProfile` (page size, facet/sort allowlists, pagination depth, field exposure), and rate-limit callers.
3. **Observe** — emit structured JSON logs and Prometheus metrics, persist an auditable usage log, and surface backend health.

> PISCO-API does not replace your source search engine, ILS, DAMS, or portal. It is a **normalizing, protective facade** that can later serve as the base for an MCP connector (see SPECS §4).

---

## Table of contents

- [Feature overview](#feature-overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [Operator CLI](#operator-cli)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Observability](#observability)
- [Public API](#public-api)
- [Admin API](#admin-api)
- [Admin web UI](#admin-web-ui)
- [Runtime paths & environment](#runtime-paths--environment)
- [Development](#development)
- [Project layout](#project-layout)
- [Scope & roadmap](#scope--roadmap)
- [License](#license)

---

## Feature overview

| Area | What you get |
| --- | --- |
| **Public API** | `GET /v1/search`, `/v1/records/{id}`, `/v1/facets`, `/v1/collections`, `/v1/schema`, `/v1/health`, `/v1/openapi.json` |
| **Optional endpoints** | `/v1/suggest` and `/v1/manifest/{id}` declared and return `501 not_implemented` (SPECS §12) |
| **Query policy** | Unknown-param rejection, sort/facet/field allowlists, hard caps on page size and pagination depth, strict boolean parsing |
| **Mapping** | 8 modes (`direct`, `split_list`, `first_non_empty`, `template`, `nested_object`, `date_parser`, `boolean_cast`, `url_passthrough`) with defensive parsing |
| **Caching** | `Cache-Control: public, max-age=<ttl>` + strong `ETag` + `If-None-Match` → `304` on all read endpoints |
| **Security profiles** | Ship with `prudent` and `standard`; add your own via config |
| **Auth modes** | Public endpoints: `anonymous_allowed` / `api_key_optional` / `api_key_required`; Admin: API key always required |
| **Rate limiting** | Per-subject sliding window for public traffic; dedicated per-IP limiter on `/admin/login` (brute-force protection) |
| **Security headers** | `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` + CSP on `/admin`, HSTS in production, CORS off by default |
| **Admin UI** | Jinja2 templates (autoescape enforced) for dashboard, config editor, mapping overview, API key management, recent activity |
| **Admin API** | Config CRUD, validation, test-query (DSL preview), paginated `/admin/v1/usage` |
| **Observability** | Prometheus `/metrics` (requests, latency, backend errors, rate-limit hits), structured JSON logs with `request_id` / `key_id` / `latency_ms` |
| **Storage** | SQLite state DB with hot-path indexes; idempotent schema migrations |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                Public caller                                │
└──────────────┬──────────────────────────────────────────────────────────────┘
               │ HTTPS
┌──────────────▼──────────────────────────────────────────────────────────────┐
│                             FastAPI application                             │
│                                                                             │
│   ┌─────────────┐    ┌──────────────────┐    ┌───────────────────────┐      │
│   │ Public API  │──▶ │ QueryPolicyEngine│──▶ │ ElasticsearchAdapter  │      │
│   │ /v1/*       │    │  (enforces       │    │  (retries, typed      │      │
│   │             │    │   SecurityProfile)│   │   errors, version gate)│     │
│   └──────┬──────┘    └────────┬─────────┘    └────────────┬──────────┘      │
│          │                    │                           │                 │
│          │              ┌─────▼───────┐           ┌───────▼────────┐        │
│          └─────────────▶│ SchemaMapper│           │ httpx (no      │        │
│                         │ (public     │           │ redirects)     │        │
│                         │  Record)    │           └───────┬────────┘        │
│                         └─────────────┘                   │                 │
│   ┌──────────────┐    ┌──────────────────┐                │                 │
│   │ Admin API    │──▶ │ ConfigManager    │                │                 │
│   │ /admin/v1/*  │    │ (YAML, redacts   │                │                 │
│   └──────┬───────┘    │  secrets)        │                │                 │
│          │            └──────────────────┘                │                 │
│   ┌──────▼───────┐    ┌──────────────────┐                │                 │
│   │ Admin UI     │    │ SQLiteStore      │                │                 │
│   │ /admin/*     │    │ (keys, sessions, │                │                 │
│   │ (Jinja2)     │    │  usage, quotas)  │                │                 │
│   └──────────────┘    └──────────────────┘                │                 │
│                                                           │                 │
│   Cross-cutting: security headers • CORS • rate limit •   │                 │
│   structured logs (structlog) • Prometheus /metrics        │                │
└────────────────────────────────────────────────────────────│────────────────┘
                                                             ▼
                                                     ┌──────────────┐
                                                     │Elasticsearch │
                                                     │  (read-only) │
                                                     └──────────────┘
```

---

## Requirements

- **Python 3.10+** (3.12 recommended).
- Read-only access to an Elasticsearch cluster (version **7+**) for real queries.
- POSIX filesystem for the SQLite state DB and the bootstrap admin key sidecar file (0600 perms).

The test suite runs fully offline with an in-memory fake adapter and `httpx.MockTransport` — no backend is required to develop or CI.

---

## Quickstart

```bash
# 1. Install (creates a virtualenv via scripts/setup.sh).
./scripts/setup.sh

# 2. Initialize config + state DB + bootstrap admin key.
pisco-api init

# 3. Start the service with auto-reload.
pisco-api run --reload
```

Open:

- **Public health**: http://127.0.0.1:8000/v1/health
- **Metrics**: http://127.0.0.1:8000/metrics
- **Admin UI**: http://127.0.0.1:8000/admin/login
- **OpenAPI**: http://127.0.0.1:8000/v1/openapi.json

The first `pisco-api init` on a fresh machine will either honour `PISCO_BOOTSTRAP_ADMIN_KEY` or generate a random admin key and store it at `data/bootstrap_admin.key` with `0600` permissions. **Copy that key now** — it is needed to log into the admin UI and to authenticate admin API calls.

---

## Operator CLI

`pisco-api` is a small wrapper around uvicorn that exposes the usual operator workflows:

| Command | Purpose |
| --- | --- |
| `pisco-api init [--force]` | Create a baseline config + state DB + bootstrap admin key |
| `pisco-api run [--host H] [--port P] [--reload]` | Start the service (default `127.0.0.1:8000`) |
| `pisco-api check-config` | Validate the on-disk config (cross-field rules included) |
| `pisco-api check-backend` | Probe the configured backend for reachability |
| `pisco-api print-paths` | Show effective config / state-db / bootstrap-key paths |

Equivalent `make` targets (`make setup`, `make init`, `make run`, `make check-config`, `make check-backend`, `make print-paths`, `make test`) are provided for convenience.

---

## Configuration

Config lives in a single YAML file (default: `config/pisco.yaml`). A fully annotated example ships in `examples/config.yaml`.

### Top-level sections

```yaml
backend:          # Where to talk to the search engine
storage:          # Where the SQLite state DB lives
security_profile: # Name of the profile applied to public requests
profiles:         # Declared profiles (prudent, standard, your-own…)
auth:             # Admin bootstrap, cookie hardening, session TTL
cors:             # off | allowlist | wide_open
cache:            # Public response TTL + kill-switch
rate_limit:       # Public + admin-login limits
allowed_sorts:    # Sort allowlist
allowed_facets:   # Facet allowlist
allowed_include_fields:  # Fields that may appear in include_fields
mapping:          # Public field → backend source rules
```

### Security profile

Profiles tune every public-request enforcement point at once:

```yaml
profiles:
  prudent:
    allow_empty_query: false
    page_size_default: 20
    page_size_max: 50
    max_facets: 3
    max_buckets_per_facet: 20
    allow_raw_fields: false
    allow_debug_translation: false
    max_depth: 2000
```

The engine rejects any request that would violate the active profile with a typed error (`invalid_parameter`, `forbidden`, `unsupported_operation`, `missing_parameter`).

### Mapping modes

| Mode | What it does |
| --- | --- |
| `direct` | `doc[source]` as-is |
| `constant` | Emits a fixed string |
| `split_list` | `doc[source].split(separator)` with trimming |
| `first_non_empty` | First truthy value across `sources` |
| `template` | `string.Template.safe_substitute(doc)` |
| `nested_object` | Pass-through if the source is an object |
| `date_parser` | ISO-8601 → `YYYY-MM-DD`; returns `None` on bad input (never raises) |
| `boolean_cast` | Python truthiness of the source value |
| `url_passthrough` | Keeps the value only if it is a well-formed `http(s)://host/...` URL |

### Cross-field validation

The config is validated by Pydantic + custom `model_validator`s that check:

- `security_profile` exists in `profiles`.
- `auth.public_mode` ∈ {`anonymous_allowed`, `api_key_optional`, `api_key_required`}.
- `cors.mode` ∈ {`off`, `allowlist`, `wide_open`}.
- Every `allowed_include_fields` entry is either structural (`id`/`type`) or declared in `mapping`.
- Any `required`/`recommended` mapping rule declares at least one of `source`, `sources`, `constant`, or `template`.

Invalid configs fail `pisco-api check-config` fast with a structured error.

---

## Security model

PISCO-API was hardened against a full audit; the highlights that matter most in production:

- **Bootstrap admin key** is never the legacy `admin-change-me`. If no key is provided via env or config, a random token is generated (dev only) and persisted at `data/bootstrap_admin.key` with `0600` perms. In production (`PISCO_ENV=production`) the service refuses to start if no explicit key is available.
- **Secrets never leak to YAML.** `ConfigManager.save()` strips `auth.bootstrap_admin_key` before writing; in-memory config keeps the secret.
- **Usage log never stores the raw API key.** The audit middleware resolves `x-api-key` to the public `key_id` label; anonymous or invalid-key traffic falls back to the client host.
- **Admin session cookie** defaults to `Secure=true` + `SameSite=strict`, with a configurable TTL (default 12 h) enforced at the DB level and purged on read.
- **Brute-force guard on `/admin/login`** — a dedicated per-IP rate limiter runs **before** credential verification (default 10 attempts / 5 min).
- **SSRF hardening** — the backend `httpx.Client` is created with `follow_redirects=False`.
- **Security headers** — `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` + CSP on `/admin`, HSTS when `PISCO_ENV=production`.
- **CORS off by default** (`cors.mode: off`); enable via `allowlist` (explicit origins) or `wide_open` (`*`, credentials disabled).
- **Admin UI is autoescaped Jinja2.** `templates.env.autoescape = True` is asserted explicitly, and no route builds HTML via string concatenation.
- **Key labels are regex-validated** (`^[a-zA-Z0-9_.-]{1,64}$`) at creation and on status-action routes.

---

## Observability

### Prometheus metrics

`GET /metrics` returns the standard Prometheus exposition format from a dedicated registry. Series exposed:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `pisco_requests_total` | Counter | `endpoint`, `method`, `status` | Every HTTP response |
| `pisco_request_duration_seconds` | Histogram | `endpoint`, `method` | Request latency (buckets 5 ms → 10 s) |
| `pisco_backend_errors_total` | Counter | `error_code` | Incremented on every backend failure |
| `pisco_rate_limit_hits_total` | Counter | `scope` (`public`/`admin`) | Incremented on every `429` |

### Structured logs

`structlog` is configured at import time to emit newline-delimited JSON to stderr. Every request produces one `"event": "request"` line with `request_id`, `method`, `path`, `status_code`, `latency_ms`, and the resolved `key_id` (never the secret). Stdlib loggers (`httpx`, `uvicorn`) share the same renderer.

Control the level via `PISCO_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` (default `INFO`).

### Usage log

Every HTTP response is also persisted in `usage_events` with `latency_ms`, `status_code`, `api_key_id`, and `subject`. The admin UI renders the last 100 rows at `/admin/ui/usage`; the admin API exposes paginated access at `GET /admin/v1/usage?limit=&offset=`.

---

## Public API

All responses are JSON. Errors follow SPECS §19:

```json
{
  "error": {
    "code": "invalid_parameter",
    "message": "page_size exceeds policy",
    "details": {"max": 50, "requested": 200},
    "request_id": "…"
  }
}
```

| Method & path | Purpose |
| --- | --- |
| `GET /v1/health` | Liveness + backend health |
| `GET /v1/search` | Full-text search with filters, sort, facets, `include_fields` |
| `GET /v1/records/{id}` | Single record by identifier |
| `GET /v1/facets` | Facet counts only (aggregations via `size=0`) |
| `GET /v1/collections` | Collections the service exposes |
| `GET /v1/schema` | Active public schema + allowlists |
| `GET /v1/suggest` | **501** (declared for SPECS §12.2; implementation pending) |
| `GET /v1/manifest/{id}` | **501** (declared for SPECS §12.3; implementation pending) |
| `GET /v1/openapi.json` | OpenAPI 3 schema |

### Example — search

```bash
curl -s 'http://127.0.0.1:8000/v1/search?q=henri+matisse&facet=type&page=1&page_size=20' | jq
```

### Example — conditional GET (304 round-trip)

```bash
etag=$(curl -s -D - 'http://127.0.0.1:8000/v1/search?q=x' -o /dev/null | awk -F': ' '/^etag:/ {print $2}' | tr -d '\r')
curl -s -o /dev/null -w '%{http_code}\n' \
     -H "If-None-Match: $etag" \
     'http://127.0.0.1:8000/v1/search?q=x'
# → 304
```

### Allowed query parameters

`q`, `page`, `page_size`, `sort`, `facet` (repeatable), `include_fields`, `type`, `collection`, `language`, `institution`, `subject`, `date_from`, `date_to`, `has_digital`, `has_iiif`. Any other key returns `400 invalid_parameter`.

---

## Admin API

All admin routes require `x-api-key: <admin-key>`.

| Method & path | Purpose |
| --- | --- |
| `POST /admin/v1/setup/detect` | Probe backend and return its version |
| `POST /admin/v1/setup/scan-fields` | Return the backend index mapping |
| `POST /admin/v1/setup/create-config` | Replace the current config with a validated payload |
| `GET  /admin/v1/config` | Read the current config (secrets redacted) |
| `PUT  /admin/v1/config` | Persist a full config replacement |
| `POST /admin/v1/config/validate` | Dry-run validation |
| `POST /admin/v1/test-query` | Translate a query to backend DSL without running it |
| `GET  /admin/v1/usage?limit=&offset=` | Paginated usage events |
| `GET  /admin/v1/status` | Backend + mapping health aggregate |

### Example

```bash
curl -s -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:8000/admin/v1/status | jq
```

---

## Admin web UI

Same-origin console at `/admin/*`, served by Jinja2 templates (autoescape enforced):

- `/admin/login` — bootstrap form (rate-limited).
- `/admin/ui` — dashboard (service + backend health, usage summary).
- `/admin/ui/config` — editable configuration form.
- `/admin/ui/mapping` — mapping and allowlists overview.
- `/admin/ui/keys` — create / suspend / revoke API keys.
- `/admin/ui/usage` — recent activity (latest 100 rows).

The session cookie is `HttpOnly`; it is `Secure` + `SameSite=strict` by default (toggle via `auth.admin_cookie_secure` / `auth.admin_cookie_samesite` for local HTTP development).

---

## Runtime paths & environment

Defaults (relative to `PISCO_HOME`, which defaults to the process CWD):

| Path | Default |
| --- | --- |
| Config file | `config/pisco.yaml` |
| State DB | `data/pisco_state.sqlite3` |
| Bootstrap admin key sidecar | `data/bootstrap_admin.key` |

Environment overrides:

| Variable | Purpose |
| --- | --- |
| `PISCO_HOME` | Base dir for default paths |
| `PISCO_CONFIG_PATH` | Absolute override for the config file |
| `PISCO_STATE_DB_PATH` | Absolute override for the SQLite state DB |
| `PISCO_BOOTSTRAP_KEY_PATH` | Absolute override for the sidecar key file |
| `PISCO_BOOTSTRAP_ADMIN_KEY` | Explicit bootstrap key (highest priority) |
| `PISCO_ENV` | `development` (default) or `production` — toggles HSTS and stricter bootstrap rules |
| `PISCO_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Run `pisco-api print-paths` to inspect effective values.

### Constrained / offline environments

```bash
python -m pip install --no-index --find-links /path/to/wheels -e .[dev]
pisco-api init
pisco-api run
```

---

## Development

```bash
./scripts/setup.sh         # create virtualenv + editable install
make test                  # run the full test suite (pytest)
pytest tests/security/     # run just the security regression suites
```

The suite ships **134 tests**, organized as follows:

```
tests/
├── conftest.py                             # fake adapter, tmp home, deterministic admin key
├── contract/test_contract.py               # OpenAPI + response-model shape
├── integration/
│   ├── test_admin_api.py                   # admin HTTP surface
│   ├── test_admin_ui.py                    # admin UI flows (login, config, keys, usage)
│   └── test_public_api.py                  # /v1/* happy paths
├── security/
│   ├── test_security.py                    # authn, allowlists, rate limiting
│   ├── test_vague1_hardening.py            # C1-C6 (bootstrap, cookie, PII, redaction, session, CORS/headers)
│   ├── test_vague2_robustness.py           # H1-H5, H11 (N+1, retries, max_depth, ETag/304, login RL, redirects)
│   ├── test_vague3_hardening.py            # H6-H8, M3-M7 (XSS, key_id regex, mapper, buckets, raw_fields, docs)
│   ├── test_vague4_observability.py        # H9-H10, M1-M2, M5 (Prometheus, structlog, indexes, pagination, ES gate)
│   └── test_vague5_spec.py                 # M8-M9, L1-L4 (cross-validators, optional endpoints, gap coverage)
└── unit/
    ├── test_cli.py                         # operator CLI
    ├── test_mapper.py                      # mapping modes
    ├── test_persistence.py                 # SQLite store
    └── test_query_policy.py                # policy engine
```

Any change to a public behavior ships with a regression test — see [CHANGELOG.md](./CHANGELOG.md) for the running record.

---

## Project layout

```
PISCO-API/
├── app/
│   ├── adapters/elasticsearch/      # httpx-based adapter with retries + version gate
│   ├── admin_api/                   # /admin/v1/* routes
│   ├── admin_ui/                    # Jinja2 templates + routes for /admin/*
│   ├── auth/                        # API key manager, admin & public dependencies
│   ├── config/                      # Pydantic models + YAML ConfigManager
│   ├── logging/                     # structlog bootstrap + request_context
│   ├── mappers/                     # SchemaMapper, defensive mode helpers
│   ├── metrics/                     # Prometheus registry + counters/histograms
│   ├── public_api/                  # /v1/* routes
│   ├── query_policy/                # SecurityProfile enforcement + cache-key
│   ├── rate_limit/                  # InMemoryRateLimiter (named constants)
│   ├── schemas/                     # Pydantic response models (Record, SearchResponse)
│   ├── storage/                     # SQLiteStore (keys, sessions, quotas, usage)
│   ├── cli.py                       # `pisco-api` entry point
│   ├── dependencies.py              # DI container with threadsafe reload()
│   ├── errors.py                    # AppError + JSON renderer
│   ├── http_cache.py                # Cache-Control + ETag + 304 helper
│   ├── main.py                      # FastAPI app, middleware chain, /metrics
│   └── runtime_paths.py             # path resolution + bootstrap-key precedence
├── examples/config.yaml             # annotated reference config
├── scripts/setup.sh                 # venv + editable install
├── tests/                           # 134 tests (see above)
├── CHANGELOG.md
├── INSTALL.md
├── LICENSE
├── Makefile
├── pyproject.toml
├── README.md
└── SPECS.md                         # full product specification (French)
```

---

## Scope & roadmap

### V1 (shipped)

- Elasticsearch adapter, read-only.
- Public `/v1/*` with query-policy enforcement.
- Admin `/admin/v1/*` and operator UI under `/admin/*`.
- YAML configuration with cross-field validation.
- SQLite state (keys, sessions, quotas, usage) with hot-path indexes.
- Security hardening (see [Security model](#security-model)).
- Prometheus metrics + structured JSON logs.
- HTTP response caching with ETag / 304.

### Declared but not implemented

- `/v1/suggest` — autocomplete (SPECS §12.2).
- `/v1/manifest/{id}` — IIIF passthrough/redirect (SPECS §12.3).

### Out of scope for V1

- OpenSearch and Solr adapters.
- Multi-tenant isolation.
- Deep-pagination workarounds (`search_after` / PIT).
- Native MCP server (SPECS explicitly lists this as future work).

Follow-ups for these are tracked in [CHANGELOG.md](./CHANGELOG.md) and the SPECS.

---

## License

See [`LICENSE`](./LICENSE).
