# PISCO-API

PISCO-API is a plug-and-play API layer for GLAM collections. It sits in front of an existing search backend and exposes a safe, normalized public API contract.

## MVP status (V1 foundation)

This repository now includes a runnable FastAPI service with:

- Public API (`/v1/*`) for search/record/facet access.
- Admin API (`/admin/v1/*`) for setup/configuration.
- Admin web UI (`/admin/*`) for operator workflows.
- YAML configuration file with validation.
- SQLite-backed persistent operational state (API keys, usage, sessions).
- Elasticsearch adapter (read-only).
- Lightweight operator CLI (`pisco-api`).

## Requirements

- Python 3.12+
- Access to an Elasticsearch backend for real queries.

## Quickstart (recommended)

```bash
./scripts/setup.sh
pisco-api init
pisco-api run --reload
```

Then open:

- Public health: `http://127.0.0.1:8000/v1/health`
- Admin login: `http://127.0.0.1:8000/admin/login`

## Operator commands

```bash
pisco-api init
pisco-api run --reload
pisco-api check-config
pisco-api check-backend
pisco-api print-paths
```

Equivalent Make targets:

```bash
make setup
make init
make run
make check-config
make check-backend
make print-paths
```

## Runtime paths

Defaults:

- Config: `config/pisco.yaml`
- State DB: `data/pisco_state.sqlite3`

Overrides:

- `PISCO_HOME` (base dir for default paths)
- `PISCO_CONFIG_PATH`
- `PISCO_STATE_DB_PATH`
- `PISCO_BOOTSTRAP_ADMIN_KEY`

Use `pisco-api print-paths` to see effective values.

## Bootstrap and first run behavior

`pisco-api init` will:

1. Create config file (unless already present).
2. Create SQLite state DB and schema.
3. Ensure an admin API key exists from `auth.bootstrap_admin_key` or `PISCO_BOOTSTRAP_ADMIN_KEY`.

`pisco-api check-config` fails fast with explicit guidance if config is missing or invalid.

## Locked-down/offline-ish environments

If you cannot access public package indexes directly:

1. Build/download wheels in an allowed environment.
2. Install with your internal index or wheelhouse.
3. Run initialization and runtime commands as normal.

Example wheelhouse install approach:

```bash
python -m pip install --no-index --find-links /path/to/wheels -e .[dev]
pisco-api init
pisco-api run
```

## Scope and constraints

V1 intentionally supports:

- Elasticsearch adapter only.
- Read-only backend operations.
- Query-policy enforcement between public API and backend.

V1 intentionally does not include:

- Multi-tenant support.
- Deep pagination workaround.
- OpenSearch/Solr adapters.

## Development

```bash
make test
```

## License

See `LICENSE`.
