# Installation and Local Operations

## 1) Install

```bash
./scripts/setup.sh
```

## 2) Initialize local runtime files

```bash
pisco-api init
```

## 3) Validate configuration

```bash
pisco-api check-config
```

## 4) Start service

```bash
pisco-api run --reload
```

## 5) Useful operations

```bash
pisco-api print-paths
pisco-api check-backend
```

## Admin access

- Admin UI login: `http://127.0.0.1:8000/admin/login`
- Admin API base: `http://127.0.0.1:8000/admin/v1`

## Public access

- Public API base: `http://127.0.0.1:8000/v1`
- Health endpoint: `GET /v1/health`

## Runtime path variables

- `PISCO_HOME`
- `PISCO_CONFIG_PATH`
- `PISCO_STATE_DB_PATH`
- `PISCO_BOOTSTRAP_ADMIN_KEY`

## Stop/restart

- Stop: `Ctrl+C`
- Restart: `pisco-api run`

## Constrained environments

If internet access is restricted, install from internal mirror/wheelhouse:

```bash
python -m pip install --no-index --find-links /path/to/wheels -e .[dev]
```

Then continue with `pisco-api init` and `pisco-api run`.
