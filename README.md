# PISCO-API

PISCO-API is a plug-and-play API layer for GLAM collections. It exposes a safe, normalized public API in front of an existing backend search index (Elasticsearch in v1).

## What this version supports

- FastAPI service with public API and admin API
- Protected admin web UI
- Elasticsearch adapter (read-only)
- YAML configuration
- SQLite operational state (API keys, quotas, usage events, admin UI sessions)
- Query policy enforcement and schema mapping

## Requirements

- Python 3.10+
- Access to an Elasticsearch backend for full end-to-end runtime checks

## Quick start (recommended)

```bash
./scripts/setup.sh
pisco-api init
pisco-api run --reload
```

Then open:
- Public health: `http://127.0.0.1:8000/v1/health`
- Admin UI login: `http://127.0.0.1:8000/admin/login`

## Operator commands

Installed command: `pisco-api`

- `pisco-api init` — create default config (if missing), initialize SQLite state DB, bootstrap admin key
- `pisco-api run [--host 127.0.0.1 --port 8000 --reload]` — start server
- `pisco-api check-config` — validate active config file
- `pisco-api check-backend` — verify backend connectivity from current config
- `pisco-api print-paths` — print effective config/state paths and key source hints

Equivalent Make targets:

```bash
make setup
make init
make run
make check-config
make check-backend
make paths
make test
```

## Path model (predictable local layout)

Default paths:
- Config YAML: `config/pisco.yaml`
- SQLite state DB: `data/pisco_state.sqlite3`

Overrides:
- `PISCO_CONFIG_PATH`
- `PISCO_STATE_DB_PATH`
- `PISCO_BOOTSTRAP_ADMIN_KEY`

Use `pisco-api print-paths` to confirm active paths.

## First run and startup behavior

- If config is missing at startup, the app fails with a clear message:
  - `Configuration file not found ... Run pisco-api init`
- `pisco-api init` creates required paths and initializes the DB schema.
- On app startup, SQLite schema is ensured automatically.
- Invalid config fails fast during startup.

## Admin UI (for non-technical operators)

Entry points:
- `/admin/login`
- `/admin/ui`

Sign in uses existing admin API key validation. On success, a short-lived server-side UI session is created and stored in SQLite.

Available pages:
- Dashboard (service/paths/usage summary)
- Configuration (safe editable subset)
- Mapping and exposure inspection
- API key management (create, suspend, revoke, activate)
- Recent activity table (usage events)

## Admin API

Base path: `/admin/v1` (API key required via `x-api-key`)

- `POST /setup/detect`
- `POST /setup/scan-fields`
- `POST /setup/create-config`
- `GET /config`
- `PUT /config`
- `POST /config/validate`
- `POST /test-query`
- `GET /status`

## Public API

Base path: `/v1`

- `GET /search`
- `GET /records/{id}`
- `GET /facets`
- `GET /health`
- `GET /openapi.json`

## Locked-down/offline installation

If internet access is blocked by proxy policy, use one of:

1) Internal package mirror

```bash
PIP_INDEX_URL=https://<internal-mirror>/simple \
python -m pip install --no-build-isolation -e '.[dev]'
```

2) Prebuilt wheelhouse

Connected environment:

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

Locked-down environment:

```bash
python -m pip install --no-index --find-links=wheelhouse --no-build-isolation -e '.[dev]'
```

## V1 limitations

- Elasticsearch only (no OpenSearch/Solr yet)
- Single-instance operational model
- No cursor pagination yet
- No web admin mapping studio (inspection-first)

## Deferred roadmap

- OpenSearch adapter
- Solr adapter
- Redis-backed rate limiting/cache
- Cursor pagination
- OAI-PMH module
- IIIF proxy hardening
