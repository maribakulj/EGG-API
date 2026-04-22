from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
import webbrowser

import uvicorn

from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.runtime_paths import (
    get_bootstrap_admin_key,
    get_config_path,
    get_home_dir,
    get_state_db_path,
)
from app.storage.sqlite_store import SQLiteStore
from app.user_errors import format_for_terminal


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
    print("Bootstrap complete. Next: egg-api start   (or: egg-api run)")
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
    except Exception as exc:
        print(
            f"Configuration check failed.\n{format_for_terminal(exc)}\n"
            "Hint: run `egg-api init` to generate a baseline config.",
            file=sys.stderr,
        )
        return 2


def cmd_check_backend(_: argparse.Namespace) -> int:
    try:
        from app.dependencies import container

        health = container.adapter.health()
        print(json.dumps({"status": "ok", "backend": health}, indent=2))
        return 0
    except Exception as exc:
        print(
            f"Backend check failed.\n{format_for_terminal(exc)}",
            file=sys.stderr,
        )
        return 3


def cmd_migrate(_: argparse.Namespace) -> int:
    cfg_path = get_config_path()
    cfg = AppConfig()
    if cfg_path.exists():
        cfg = ConfigManager(cfg_path, require_existing=True).config
    db_path = get_state_db_path(cfg.storage.sqlite_path)
    store = SQLiteStore(db_path)
    with store._connect() as conn:
        from app.storage.migrations import current_version, migrate

        before = current_version(conn)
        applied = migrate(conn)
        after = current_version(conn)
    output = {
        "db_path": str(db_path),
        "before": before,
        "after": after,
        "applied": [{"version": m.version, "name": m.name} for m in applied],
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _schedule_browser_open(url: str, delay_seconds: float) -> None:
    """Open ``url`` in the default browser once uvicorn has had time to bind.

    Fire-and-forget: we don't care if the call fails (headless server,
    no desktop, ssh session). The magic link is also printed to stdout
    so the operator always has a fallback.
    """

    def _open() -> None:
        time.sleep(delay_seconds)
        # Ignore any failure: headless env, no desktop, blocked by
        # firewall. The magic link was already printed to the terminal.
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    thread = threading.Thread(target=_open, daemon=True)
    thread.start()


def cmd_start(args: argparse.Namespace) -> int:
    """First-run-friendly server start: init + OTP magic link + uvicorn.

    Idempotent: running it a second time does not regenerate the
    bootstrap key or overwrite the config file. Always prints the
    magic link so headless-server operators can copy-paste it.
    """
    # 1. Make sure config + state DB exist.
    config_path = get_config_path()
    manager = ConfigManager(config_path, require_existing=False)
    if not config_path.exists():
        cfg = AppConfig()
        cfg.storage.sqlite_path = str(get_state_db_path(cfg.storage.sqlite_path))
        manager.save(cfg)

    cfg = manager.load() if config_path.exists() else AppConfig()
    db_path = get_state_db_path(cfg.storage.sqlite_path)
    store = SQLiteStore(db_path)
    store.initialize()

    bootstrap_key = get_bootstrap_admin_key(cfg.auth.bootstrap_admin_key)
    store.ensure_admin_key(bootstrap_key)

    # 2. Mint a one-time magic link.
    otp = store.create_setup_otp("admin", ttl_seconds=300)
    magic_url = f"http://{args.host}:{args.port}/admin/setup-otp/{otp}"

    # 3. Show the operator exactly what they need.
    print("\nEGG-API — first-run launcher")
    print("----------------------------")
    print(f"Admin key (keep this safe — used for CLI/API access):\n  {bootstrap_key}\n")
    print("One-time setup link (opens the wizard, expires in 5 minutes):")
    print(f"  {magic_url}\n")
    if not args.no_browser:
        print("Opening your default browser automatically…")
        _schedule_browser_open(magic_url, delay_seconds=1.5)
    print(
        f"Starting the API server on http://{args.host}:{args.port}   (Ctrl+C to stop)\n",
        flush=True,
    )

    # 4. Drop into uvicorn.
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
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

    start = sub.add_parser(
        "start",
        help="First-run-friendly launcher: init + magic link + browser + server",
    )
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8000)
    start.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip the automatic webbrowser.open() call (useful on headless hosts)",
    )
    start.set_defaults(func=cmd_start)

    check_config = sub.add_parser("check-config", help="Validate current configuration file")
    check_config.set_defaults(func=cmd_check_config)

    check_backend = sub.add_parser(
        "check-backend", help="Check backend connectivity using current config"
    )
    check_backend.set_defaults(func=cmd_check_backend)

    show = sub.add_parser("print-paths", help="Print effective config/state paths")
    show.set_defaults(func=cmd_print_paths)

    migrate = sub.add_parser("migrate", help="Apply pending schema migrations")
    migrate.set_defaults(func=cmd_migrate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
