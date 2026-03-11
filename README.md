# PISCO-API (MVP -> V1 Core Foundation)

PISCO-API is a plug-and-play API layer for GLAM collections. It sits in front of an existing search backend and exposes a safe, normalized, backend-independent public API contract.

## Purpose

This project provides a production-oriented baseline for institutions that already have searchable data (Elasticsearch in v1 scope) and need a stable public API, admin controls, and safe query behavior.

## Architecture Overview

- **FastAPI app** (`app/main.py`)
- **Public API** (`app/public_api/routes.py`)
- **Admin API** (`app/admin_api/routes.py`)
- **Query policy layer** (`app/query_policy/engine.py`)
- **Backend adapter** (`app/adapters/elasticsearch/adapter.py`)
- **Schema mapping** (`app/mappers/schema_mapper.py`)
- **YAML config manager** (`app/config/manager.py`)
- **Persistent operational state (SQLite)** (`app/storage/sqlite_store.py`)
- **API key manager** (`app/auth/api_keys.py`)
- **Persistent rate limiter** (`app/rate_limit/limiter.py`)

## Current Scope (v1)

Included:
- Elasticsearch adapter
- Read-only public endpoints
- Admin setup/config endpoints
- YAML config persistence
- SQLite-backed operational state
- Persistent API keys and key status
- Persistent quota counters + usage events
- Tests (unit + integration + security + contract)

Not included in v1:
- OpenSearch adapter
- Solr adapter
- Web admin UI
- Redis-backed rate limiting/cache
- cursor pagination
- OAI-PMH module
- IIIF proxy enhancements

## Runtime requirements

- Python 3.10+
- Elasticsearch backend (v8+ recommended)

## Configuration and storage paths

By default:
- **Config YAML path:** `examples/config.yaml`
- **SQLite state DB path:** `data/pisco_state.sqlite3`

Environment overrides:
- `PISCO_CONFIG_PATH` → override YAML config location
- `PISCO_STATE_DB_PATH` → override SQLite state DB location
- `PISCO_BOOTSTRAP_ADMIN_KEY` → override bootstrap admin key (recommended in non-dev environments)

What persists in SQLite:
- API key metadata + hashed secrets + key status (`active`/`revoked`/`suspended`)
- quota configuration
- quota counters
- usage/audit events (request_id, timestamp, endpoint, method, status_code, key identity marker, latency, error code)

What persists in YAML:
- runtime application configuration (backend, policy profile, allowlists, mapping, storage path defaults)

## Startup/bootstrap behavior

- On startup, the app validates config and initializes SQLite schema automatically.
- If configuration is invalid, startup fails explicitly.
- No external infrastructure is required for default local development.

## Locked-down/offline install strategy

If your environment cannot access public package indexes (proxy blocks `pypi.org`), use one of these:

1. **Internal mirror (recommended)**

   ```bash
   PIP_INDEX_URL=https://<internal-mirror>/simple \
   python -m pip install --no-build-isolation -e '.[dev]'
   ```

2. **Prebuilt wheelhouse (fully offline)**

   In a connected environment:

   ```bash
   mkdir -p wheelhouse
   python -m pip wheel \
     fastapi>=0.115.0 \
     uvicorn>=0.30.0 \
     pydantic>=2.8.0 \
     httpx>=0.27.0 \
     PyYAML>=6.0.1 \
     pytest>=8.2.0 \
     pytest-asyncio>=0.23.0 \
     -w wheelhouse
   ```

   In the locked-down environment:

   ```bash
   python -m pip install --no-index --find-links=wheelhouse --no-build-isolation -e '.[dev]'
   ```

## Quickstart

```bash
./scripts/setup.sh
pytest
uvicorn app.main:app --reload --port 8000
```

## Public API

Base: `/v1`

- `GET /v1/search`
- `GET /v1/records/{id}`
- `GET /v1/facets`
- `GET /v1/health`
- `GET /v1/openapi.json`

Public query parameters are normalized through `QueryPolicyEngine`; raw backend DSL is never exposed.

## Admin API

Base: `/admin/v1` (requires `x-api-key`)

- `POST /setup/detect`
- `POST /setup/scan-fields`
- `POST /setup/create-config`
- `GET /config`
- `PUT /config`
- `POST /config/validate`
- `POST /test-query`
- `GET /status`

`GET /admin/v1/config` exposes both config and active path locations (`config_path`, `state_db_path`).
`GET /admin/v1/status` includes usage summary sourced from persistent SQLite usage events.

## Security model

- Read-only backend adapter behavior
- Admin endpoints require valid API key
- Public auth mode configurable:
  - `anonymous_allowed`
  - `api_key_optional`
  - `api_key_required`
- Persistent rate limiting counters enforce quotas across restarts
- Unknown params, unsupported sorts/facets, and unsafe deep pagination are rejected explicitly
- API key secrets are stored hashed in SQLite (never plaintext)

## Roadmap

- OpenSearch adapter
- Solr adapter
- Web admin UI
- Redis-backed rate limiting
- Redis-backed caching
- Cursor pagination
- OAI-PMH module
- IIIF proxy hardening
