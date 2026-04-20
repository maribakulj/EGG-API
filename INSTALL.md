# Installation and Local Operations

## 1) Install

```bash
./scripts/setup.sh
```

## 2) Initialize local runtime files

```bash
egg-api init
```

## 3) Validate configuration

```bash
egg-api check-config
```

## 4) Start service

```bash
egg-api run --reload
```

## 5) Useful operations

```bash
egg-api print-paths
egg-api check-backend
```

## Admin access

- Admin UI login: `http://127.0.0.1:8000/admin/login`
- Admin API base: `http://127.0.0.1:8000/admin/v1`

## Public access

- Public API base: `http://127.0.0.1:8000/v1`
- Health endpoint: `GET /v1/health`

## Runtime path variables

- `EGG_HOME`
- `EGG_CONFIG_PATH`
- `EGG_STATE_DB_PATH`
- `EGG_BOOTSTRAP_ADMIN_KEY`

## Stop/restart

- Stop: `Ctrl+C`
- Restart: `egg-api run`

## Constrained environments

If internet access is restricted, install from internal mirror/wheelhouse:

```bash
python -m pip install --no-index --find-links /path/to/wheels -e .[dev]
```

Then continue with `egg-api init` and `egg-api run`.
