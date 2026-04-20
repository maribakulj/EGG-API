from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.runtime_paths import get_bootstrap_admin_key, get_config_path, get_home_dir, get_state_db_path
from app.storage.sqlite_store import SQLiteStore


def cmd_init(args: argparse.Namespace) -> int:
    config_path = get_config_path()
    manager = ConfigManager(config_path, require_existing=False)

    if config_path.exists() and not args.force:
        print(f"Config already exists at {config_path}. Use --force to overwrite.")
    else:
        cfg = AppConfig()
        cfg.storage.sqlite_path = str(get_state_db_path(cfg.storage.sqlite_path))
        manager.save(cfg)
        print(f"Wrote config: {config_path}")

    cfg = manager.load() if config_path.exists() else AppConfig()
    db_path = get_state_db_path(cfg.storage.sqlite_path)
    store = SQLiteStore(db_path)
    store.initialize()

    bootstrap_key = get_bootstrap_admin_key(cfg.auth.bootstrap_admin_key)
    store.ensure_admin_key(bootstrap_key)

    print(f"Initialized state DB: {db_path}")
    print("Bootstrap complete. Next: egg-api run --reload")
    return 0


def cmd_print_paths(_: argparse.Namespace) -> int:
    cfg_path = get_config_path()
    cfg_exists = cfg_path.exists()
    cfg = AppConfig()
    if cfg_exists:
        cfg = ConfigManager(cfg_path, require_existing=True).config

    output = {
        "home_dir": str(get_home_dir()),
        "config_path": str(cfg_path),
        "config_exists": cfg_exists,
        "state_db_path": str(get_state_db_path(cfg.storage.sqlite_path)),
        "bootstrap_admin_key_source": "env:EGG_BOOTSTRAP_ADMIN_KEY"
        if "EGG_BOOTSTRAP_ADMIN_KEY" in os.environ
        else "config.auth.bootstrap_admin_key",
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_check_config(_: argparse.Namespace) -> int:
    cfg_path = get_config_path()
    try:
        manager = ConfigManager(cfg_path, require_existing=True)
        print(f"Configuration is valid: {manager.path}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            "Configuration check failed: "
            f"{exc}. Hint: run `egg-api init` to generate a baseline config.",
            file=sys.stderr,
        )
        return 2


def cmd_check_backend(_: argparse.Namespace) -> int:
    try:
        from app.dependencies import container

        health = container.adapter.health()
        print(json.dumps({"status": "ok", "backend": health}, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            "Backend check failed: "
            f"{exc}. Verify backend.url/index in config and network reachability.",
            file=sys.stderr,
        )
        return 3


def cmd_run(args: argparse.Namespace) -> int:
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="egg-api", description="EGG-API operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create default config/state files and initialize storage")
    init.add_argument("--force", action="store_true", help="Overwrite existing config file")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="Run the API server")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--reload", action="store_true")
    run.set_defaults(func=cmd_run)

    check_config = sub.add_parser("check-config", help="Validate current configuration file")
    check_config.set_defaults(func=cmd_check_config)

    check_backend = sub.add_parser("check-backend", help="Check backend connectivity using current config")
    check_backend.set_defaults(func=cmd_check_backend)

    show = sub.add_parser("print-paths", help="Print effective config/state paths")
    show.set_defaults(func=cmd_print_paths)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
