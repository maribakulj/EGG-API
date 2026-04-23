# EGG-API — Easy GLAM Gateway API

**A plug-and-play, safely-exposed public API for the collections of small heritage institutions (GLAM: Galleries, Libraries, Archives, Museums).**

EGG-API exists for a simple reason: small GLAM institutions have *data* — a catalog, an ILS, a DAMS, an Elasticsearch index — but rarely a software team. They still deserve to publish a clean, stable, secured public API on top of that data, without hiring a developer and without rewriting the backend they already run.

EGG-API is the middle layer that makes this possible. It sits between the public and the existing backend, learns how to read it through **standard configuration menus** (no code, no custom query DSL to learn), and exposes a normalized, safe API in front of it.

### What it does today

1. **Normalize** — turn heterogeneous backend records into a stable public `Record` schema via a configuration-driven mapper (no code required, just a YAML or an admin-UI form).
2. **Protect** — never expose the raw backend query DSL, enforce a configurable `SecurityProfile` (page size, facet/sort allowlists, pagination depth, field exposure), and rate-limit callers.
3. **Observe** — emit structured JSON logs and Prometheus metrics, persist an auditable usage log, and surface backend health.

> EGG-API does not replace your source search engine, ILS, DAMS, or portal. It is a **normalizing, protective facade** that plugs on top of what you already run, and that can later serve as the base for an MCP connector (see SPECS §4).

### Who it is for

- **Small archives, libraries, museums, galleries** that host their own data but do not have IT or software-engineering staff on hand.
- **Librarians, archivists, documentalists, curators** who need to expose their holdings to a portal, a partner, or an aggregator, and want to do so *safely* without improvising a custom API.
- **Consortiums and aggregators** that need a predictable, stable contract regardless of which backend each member institution happens to run.

You should **not** need to write Python, a query DSL, or a REST spec to deploy it. If you can edit a settings form, you can run EGG-API.

A public **landing page** (Sprint 28) lives at `/` and introduces EGG
to first-time visitors — three collection profiles (library / museum /
archive), nine importers, the outbound OAI-PMH provider, and a
*Start the setup wizard* CTA. `/about` explains design principles
and positioning. Both pages render without JavaScript and degrade
gracefully when the backend is offline.

**Bilingual by default (Sprint 29).** The landing page and `/about`
render in English or French depending on a `?lang=fr` / `?lang=en`
query parameter, an `egg_lang` cookie, or the browser's
`Accept-Language` header. An `EGG_DEFAULT_LANG=fr` environment
variable lets a francophone deployment (Koha / PMB / AtoM / Mnesys /
Ligeo) boot directly in French.

### Current delivery & desktop roadmap

**Today**, EGG-API ships as a FastAPI service with a web-based admin UI at
`/admin/*`. Bringing it up still requires a terminal, Python 3.10+, and one
YAML edit (or a sequence of admin-API calls). In other words: the current
release is aimed at an operator, not at an archivist working alone.

**The roadmap** toward the original product promise is explicit and tracked:

1. A guided **Admin UI setup wizard** (SPECS §26, 7 screens) covering
   backend connection, source selection, field mapping, security profile,
   exposure, keys and a live test — so configuration stops requiring YAML.
2. A **desktop package** (`.msi` / `.pkg` / `.AppImage`) built with
   Briefcase + `pywebview`, launching the same FastAPI runtime in a native
   window on localhost.
3. A **first-run UX** (`egg-api start`) that generates the admin key,
   opens the wizard in the default browser via a one-time token, and
   persists runtime data under the OS-native user directory.

Until those land, the honest description of EGG-API is: a hardened,
well-tested façade for GLAM backends that still expects a Python-literate
operator for the initial install. The rest of this README describes that
reality. Progress toward the desktop story is tracked in
[CHANGELOG.md](./CHANGELOG.md) and the SPECS.

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
| **Public API** | `GET /v1/search`, `/v1/records/{id}`, `/v1/facets`, `/v1/suggest`, `/v1/collections`, `/v1/schema`, `/v1/auth/whoami`, `/v1/livez`, `/v1/readyz`, `/v1/openapi.json` |
| **Response formats** | JSON default; `?format=csv` on `/v1/search`; JSON-LD via `Accept: application/ld+json` or `?format=jsonld` |
| **Pagination** | `page` + `page_size` under `max_depth`; opaque `cursor` (base64 of `search_after`) above it, with `next_cursor` in the response |
| **Query policy** | Unknown-param rejection, sort/facet/field allowlists, hard caps on `q`/filter/`include_fields`, strict boolean parsing |
| **Mapping** | 9 modes (`direct`, `constant`, `split_list`, `first_non_empty`, `template`, `nested_object`, `date_parser`, `boolean_cast`, `url_passthrough`) with dispatch-dict handlers |
| **Caching** | `Cache-Control` + strong `ETag` + `If-None-Match` → `304`; `private` when auth is required, `public` when anonymous is allowed |
| **Security profiles** | Ship with `prudent` and `standard`; add your own via config |
| **Auth modes** | Public: `anonymous_allowed` / `api_key_optional` / `api_key_required`; Admin: API key always required; optional HMAC pepper for stored hashes (`EGG_API_KEY_PEPPER`) |
| **Rate limiting** | Per-subject limiter for public traffic (in-memory by default, Redis opt-in via `EGG_RATE_LIMIT_REDIS_URL`); dedicated per-IP limiter on `/admin/login` |
| **Security headers** | `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` + CSP on `/admin`, HSTS in production, CORS off by default; `/docs` hidden in production |
| **Admin UI** | Jinja2 autoescape, CSRF double-submit on every POST, rotate + revoke + sign-out-everywhere flows |
| **Admin API** | Config CRUD, validation, test-query + `/admin/v1/debug/translate` (DSL preview), paginated `/admin/v1/usage`, `/admin/v1/storage/stats` |
| **Observability** | Prometheus `/metrics` (auth-gated in prod), structured JSON logs with `request_id` / `key_id` / `trace_id` / `span_id` / `latency_ms`; opt-in OpenTelemetry via `EGG_OTEL_ENDPOINT` |
| **Storage** | SQLite state DB with hot-path indexes; versioned migrations (`egg-api migrate`); background retention purge |
| **Backends** | Elasticsearch (7+) or OpenSearch (1+) via `backend.type`; `BackendAdapter` Protocol for adding new backends (see `docs/backends.md`). Solr is planned (`app/TODO.md`). |

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
egg-api init

# 3. Start the service with auto-reload.
egg-api run --reload
```

Open:

- **Public health**: http://127.0.0.1:8000/v1/health
- **Metrics**: http://127.0.0.1:8000/metrics
- **Admin UI**: http://127.0.0.1:8000/admin/login
- **OpenAPI**: http://127.0.0.1:8000/v1/openapi.json

The first `egg-api init` on a fresh machine will either honour `EGG_BOOTSTRAP_ADMIN_KEY` or generate a random admin key and store it at `data/bootstrap_admin.key` with `0600` permissions. **Copy that key now** — it is needed to log into the admin UI and to authenticate admin API calls.

---

## Operator CLI

`egg-api` is a small wrapper around uvicorn that exposes the usual operator workflows:

| Command | Purpose |
| --- | --- |
| `egg-api init [--force]` | Create a baseline config + state DB + bootstrap admin key |
| `egg-api run [--host H] [--port P] [--reload]` | Start the service (default `127.0.0.1:8000`) |
| `egg-api check-config` | Validate the on-disk config (cross-field rules included) |
| `egg-api check-backend` | Probe the configured backend for reachability |
| `egg-api print-paths` | Show effective config / state-db / bootstrap-key paths |

Equivalent `make` targets (`make setup`, `make init`, `make dev` for the auto-reload loop, `make run` for a production-style local start, `make check-config`, `make check-backend`, `make print-paths`, `make test`) are provided for convenience.

---

## Configuration

Config lives in a single YAML file (default: `config/egg.yaml`). A fully annotated example ships in `examples/config.yaml`.

### Top-level sections

```yaml
backend:          # Where to talk to the search engine
storage:          # Where the SQLite state DB lives
schema_profile:   # library | museum | archive | custom (default: library)
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

The `schema_profile` knob (Sprint 23 + 26) widens the public Record
shape when the deployment needs it:
- `museum` adds `museum: { inventory_number, artist, medium, dimensions,
  acquisition_date, current_location }` and enables the IIIF passthrough
  at `/v1/manifest/{id}` when `links.iiif_manifest` is mapped.
- `archive` adds `archive: { unit_id, unit_level, extent, repository,
  scope_content, access_conditions, parent_id }` — populated by EAD
  imports (Sprint 26) or by any backend that can surface those fields.
- `library` keeps the lean shape (`id, type, title, description,
  creators`).
- `custom` disables the auto-suggest hints for operators who want to
  drive every mapping rule by hand.

Mapping keys may use a dotted form (`museum.inventory_number`,
`archive.scope_content`, `links.iiif_manifest`) to feed the sub-blocks;
an empty sub-block is dropped from the response so a library deployment
never sees a stray `museum: {}` or `archive: {}`.

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

Invalid configs fail `egg-api check-config` fast with a structured error.

---

## Security model

EGG-API was hardened against a full audit; the highlights that matter most in production:

- **Bootstrap admin key** is never the legacy `admin-change-me`. If no key is provided via env or config, a random token is generated (dev only) and persisted at `data/bootstrap_admin.key` with `0600` perms. In production (`EGG_ENV=production`) the service refuses to start if no explicit key is available.
- **Secrets never leak to YAML.** `ConfigManager.save()` strips `auth.bootstrap_admin_key` before writing; in-memory config keeps the secret.
- **Usage log never stores the raw API key.** The audit middleware resolves `x-api-key` to the public `key_id` label; anonymous or invalid-key traffic falls back to the client host.
- **Admin session cookie** defaults to `Secure=true` + `SameSite=strict`, with a configurable TTL (default 12 h) enforced at the DB level and purged on read.
- **Brute-force guard on `/admin/login`** — a dedicated per-IP rate limiter runs **before** credential verification (default 10 attempts / 5 min).
- **SSRF hardening** — the backend `httpx.Client` is created with `follow_redirects=False`.
- **Security headers** — `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` + CSP on `/admin`, HSTS when `EGG_ENV=production`.
- **CORS off by default** (`cors.mode: off`); enable via `allowlist` (explicit origins) or `wide_open` (`*`, credentials disabled).
- **Admin UI is autoescaped Jinja2.** `templates.env.autoescape = True` is asserted explicitly, and no route builds HTML via string concatenation.
- **Key labels are regex-validated** (`^[a-zA-Z0-9_.-]{1,64}$`) at creation and on status-action routes.

---

## Observability

### Prometheus metrics

`GET /metrics` returns the standard Prometheus exposition format from a dedicated registry. Series exposed:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `egg_requests_total` | Counter | `endpoint`, `method`, `status` | Every HTTP response |
| `egg_request_duration_seconds` | Histogram | `endpoint`, `method` | Request latency (buckets 5 ms → 10 s) |
| `egg_backend_errors_total` | Counter | `error_code` | Incremented on every backend failure |
| `egg_rate_limit_hits_total` | Counter | `scope` (`public`/`admin`) | Incremented on every `429` |

### Structured logs

`structlog` is configured at import time to emit newline-delimited JSON to stderr. Every request produces one `"event": "request"` line with `request_id`, `method`, `path`, `status_code`, `latency_ms`, and the resolved `key_id` (never the secret). Stdlib loggers (`httpx`, `uvicorn`) share the same renderer.

Control the level via `EGG_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` (default `INFO`).

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
| `GET /v1/suggest` | Autocomplete over a title-like field (SPECS §12.2) |
| `GET /v1/openapi.json` | OpenAPI 3 schema |
| `GET /v1/manifest/{id}` | 302 redirect to the record's IIIF manifest (museum profile) |
| `GET /v1/oai` | OAI-PMH 2.0 provider (Sprint 27) |

> **OAI-PMH provider (Sprint 27)** — EGG now re-exposes its own indexed
> content as an OAI-PMH endpoint so aggregators (Europeana, Gallica,
> Isidore, BASE, OpenAIRE, CollEx) can harvest from it. Unauthenticated
> by protocol contract, Dublin Core (`oai_dc`) metadataPrefix, supports
> the six verbs + resumption tokens for paging. Try
> `GET /v1/oai?verb=Identify` or
> `GET /v1/oai?verb=ListRecords&metadataPrefix=oai_dc`.

> **IIIF passthrough (Sprint 23)** — when the museum schema profile maps
> `links.iiif_manifest` to a backend field, `GET /v1/manifest/{id}` returns
> a `302` redirect to the upstream manifest URL the institution already
> hosts. EGG-API never proxies, parses or re-serves the manifest itself,
> so it stays out of the IIIF hosting business while keeping the
> `/v1/manifest/{id}` URI shape that IIIF clients expect. Returns `404`
> when the record is missing or has no manifest URL.

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
| `GET  /admin/v1/keys` | List every API key (never returns the raw secret) |
| `POST /admin/v1/keys` | Create a key. Returns the raw secret **once** |
| `GET  /admin/v1/keys/{key_id}` | Fetch a single key's public record |
| `PATCH /admin/v1/keys/{key_id}` | `{"action": "activate\|suspend\|revoke\|rotate"}` |
| `DELETE /admin/v1/keys/{key_id}` | Soft-delete: revoke + invalidate sessions |
| `GET  /admin/v1/logs?…` | Filterable structured-log query (SPECS §13.12) |
| `GET  /admin/v1/export-config` | Dump the active config as redacted YAML |
| `POST /admin/v1/import-config` | Validate + swap to a new config body (JSON) |

### Example

```bash
curl -s -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:8000/admin/v1/status | jq

# Create a partner key and capture the one-time secret.
curl -s -X POST -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
     -d '{"key_id": "partner_a"}' \
     http://127.0.0.1:8000/admin/v1/keys | jq

# Rotate it later; the new secret is returned in the response body.
curl -s -X PATCH -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
     -d '{"action": "rotate"}' \
     http://127.0.0.1:8000/admin/v1/keys/partner_a | jq
```

---

## Admin web UI

Same-origin console at `/admin/*`, served by Jinja2 templates (autoescape enforced):

- `/admin/login` — bootstrap form (rate-limited).
- `/admin/setup-otp/{token}` — one-time magic-link login minted by
  `egg-api start` (Sprint 16). Single-use, 5-minute TTL, hashed at
  rest. Use the CLI instead of hand-crafting URLs.
- `/admin/ui` — dashboard (service + backend health, usage summary).
- `/admin/ui/setup` — **setup wizard** (8 steps, SPECS §26): a guided
  flow covering backend → source → mapping → security → exposure →
  first public key → live test → review & publish. Step 1 includes a
  *Detect a backend on this machine* button that probes loopback +
  common docker-compose hostnames in parallel so the operator rarely
  has to type a URL. Extend the allowlist via `EGG_DISCOVERY_HOSTS`
  when ES lives on a known internal hostname. Nothing reaches
  `config/egg.yaml` until the operator clicks *Publish*; drafts are
  per-admin and survive disconnects.
- `/admin/ui/imports` — **Data imports** (Sprint 22-26): connect your
  library, museum or archive catalogue (Koha, PMB, AtoM, Axiell,
  MuseumPlus, TMS, Micromusée, Mobydoc, CollectionSpace, Orphée,
  Aleph, Symphony, Mnesys, Ligeo, ArchivesSpace, PLEADE, …) to
  EGG-API and harvest records into the active backend. Nine importer
  kinds are available:
  - **OAI-PMH — Dublin Core** (S22): universal SIGB/OAI protocol.
  - **OAI-PMH — LIDO** (S24): same protocol with the LIDO museum
    metadata prefix. Maps into the museum schema profile.
  - **OAI-PMH — MARCXML** (S25): for catalogues that expose
    MARCXML over OAI (typical of Aleph / Symphony / Koha). Flavor
    (MARC21 / UNIMARC) chosen per source.
  - **OAI-PMH — EAD** (S26): archive finding aids over OAI. One
    OAI record expands to many backend documents (archdesc root
    + every component); each carries a `parent_id` pointer.
  - **LIDO — flat XML file** (S24): absolute filesystem path, no
    OAI envelope.
  - **MARC — binary `.mrc` (ISO 2709)** (S25): the raw MARC
    export format. Supports MARC21 and UNIMARC flavors without
    `pymarc` — the parser is stdlib-only.
  - **MARCXML — flat XML file** (S25): MARCXML without OAI.
  - **CSV — flat spreadsheet** (S25): save from Excel /
    LibreOffice as UTF-8 CSV, name one column `id`, EGG ingests.
    Semicolon, tab and comma dialects are all sniffed; plural
    columns (`creators`, `subject`, …) accept the `|` separator.
  - **EAD — flat XML file** (S26): archive finding aids (EAD 2002
    or EAD3) served up as a single XML file. Same tree expansion
    as the OAI variant.

  Every source can carry an optional **Run schedule** (``hourly`` /
  ``every 6 hours`` / ``daily`` / ``weekly``; Sprint 27). A
  background polling thread picks due sources automatically — set
  the cadence once and EGG keeps the catalogue fresh without the
  operator touching the button. ``EGG_SCHEDULER=off`` disables the
  polling loop per deployment; ``EGG_SCHEDULER_TICK_SECONDS`` tunes
  how often it polls (default ``60``).
- `/admin/ui/help` — glossary of the technical terms used across the
  console, written for non-technical operators.
- `/admin/ui/config` — editable configuration form.
- `/admin/ui/mapping` — mapping and allowlists overview.
- `/admin/ui/keys` — create / suspend / revoke API keys.
- `/admin/ui/usage` — recent activity (latest 100 rows).

The session cookie is `HttpOnly`; it is `Secure` + `SameSite=strict` by default (toggle via `auth.admin_cookie_secure` / `auth.admin_cookie_samesite` for local HTTP development).

---

## Runtime paths & environment

Defaults (relative to `EGG_HOME`, which defaults to the process CWD):

| Path | Default |
| --- | --- |
| Config file | `config/egg.yaml` |
| State DB | `data/egg_state.sqlite3` |
| Bootstrap admin key sidecar | `data/bootstrap_admin.key` |

Environment overrides:

| Variable | Purpose |
| --- | --- |
| `EGG_HOME` | Base dir for default paths |
| `EGG_CONFIG_PATH` | Absolute override for the config file |
| `EGG_STATE_DB_PATH` | Absolute override for the SQLite state DB |
| `EGG_BOOTSTRAP_KEY_PATH` | Absolute override for the sidecar key file |
| `EGG_BOOTSTRAP_ADMIN_KEY` | Explicit bootstrap key (highest priority) |
| `EGG_ENV` | `development` (default) or `production` — toggles HSTS and stricter bootstrap rules |
| `EGG_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Run `egg-api print-paths` to inspect effective values.

### Constrained / offline environments

```bash
python -m pip install --no-index --find-links /path/to/wheels -e .[dev]
egg-api init
egg-api run
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
EGG-API/
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
│   ├── cli.py                       # `egg-api` entry point
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

- Elasticsearch **and OpenSearch** adapters, read-only.
- Public `/v1/*` with query-policy enforcement, including `/v1/suggest`.
- Admin `/admin/v1/*` and operator UI under `/admin/*`.
- YAML configuration with cross-field validation.
- SQLite state (keys, sessions, quotas, usage) with hot-path indexes.
- Security hardening (see [Security model](#security-model)).
- Prometheus metrics + structured JSON logs + opt-in OpenTelemetry.
- HTTP response caching with ETag / 304; JSON-LD and CSV output flavours.
- Rate limiting (in-memory by default, Redis opt-in).

### Retired in v1.0.0, restored in Sprint 23

- `GET /v1/manifest/{id}` was retired in v1.0.0 and **restored in
  Sprint 23** as a thin `302` redirect (not a proxy) to the record's
  `links.iiif_manifest` value. Available when the deployment uses the
  `museum` schema profile and maps a manifest URL on the backend
  record. See CHANGELOG.

### Out of scope for V1

- Solr adapter (planned, tracked in `app/TODO.md`).
- Multi-tenant isolation.
- Deep-pagination workarounds (`search_after` / PIT).
- Native MCP server (SPECS explicitly lists this as future work).
- Bundled desktop installer (see [Current delivery & desktop roadmap](#current-delivery--desktop-roadmap)).

Follow-ups for these are tracked in [CHANGELOG.md](./CHANGELOG.md) and the SPECS.

---

## License

See [`LICENSE`](./LICENSE).
