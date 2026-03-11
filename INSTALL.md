# Installation and Local Operations

## Standard flow

```bash
./scripts/setup.sh
pisco-api init
pisco-api run --reload
```

## Useful checks

```bash
pisco-api print-paths
pisco-api check-config
pisco-api check-backend
```

## Environment variables

- `PISCO_CONFIG_PATH`
- `PISCO_STATE_DB_PATH`
- `PISCO_BOOTSTRAP_ADMIN_KEY`

## Stop / restart

- Stop foreground server with `Ctrl+C`
- Restart with `pisco-api run` (or `make run`)

## Admin UI

- Login: `http://127.0.0.1:8000/admin/login`
- Uses admin API key for sign-in
