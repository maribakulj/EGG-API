"""Desktop launcher (Sprint 17).

Entry point for the Briefcase-packaged EGG-API desktop app. Runs
uvicorn in a daemon thread, primes the config + state DB on first
launch, mints a magic link, and opens it inside a pywebview window.

The launcher is designed so that running it without ``pywebview``
installed still does something sensible: the magic link is printed
and the server keeps running, so an operator can paste the URL into
any browser. The desktop extra pulls in ``pywebview``; in the frozen
Briefcase build it is bundled.

No ``import webview`` at module scope: that lets ``app.desktop``
itself be imported during tests on hosts that don't have pywebview.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from app.config.manager import ConfigManager
from app.config.models import AppConfig
from app.runtime_paths import (
    desktop_home_dir,
    get_bootstrap_admin_key,
    get_config_path,
    get_state_db_path,
)
from app.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("egg.desktop")


DEFAULT_PORT_RANGE_START = 8765
DEFAULT_PORT_RANGE_SIZE = 64


def ensure_desktop_home() -> Path:
    """Pin ``EGG_HOME`` to the OS-native user data dir if unset.

    Creates the directory so the first config write does not race the
    mkdir. Safe to call multiple times: an operator-provided
    ``EGG_HOME`` takes precedence.
    """
    if os.environ.get("EGG_HOME"):
        return Path(os.environ["EGG_HOME"]).expanduser()
    home = desktop_home_dir()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["EGG_HOME"] = str(home)
    return home


def find_free_port(
    start: int = DEFAULT_PORT_RANGE_START,
    count: int = DEFAULT_PORT_RANGE_SIZE,
    host: str = "127.0.0.1",
) -> int:
    """Return the first TCP port in ``[start, start+count)`` we can bind.

    The desktop app runs a loopback server, so we prefer a deterministic
    window of high ports (above the OS ephemeral range on most
    platforms) and fall back to ``0`` only when the entire window is
    busy.
    """
    for offset in range(count):
        candidate = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return candidate
    # Fallback: ask the kernel for any free port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def prepare_state_and_mint_otp(*, ttl_seconds: int = 300) -> tuple[str, str]:
    """Make sure the config/DB exist, then mint a magic-link OTP.

    Returns ``(bootstrap_key, otp)``. The caller is expected to build
    the URL and log it for the operator; never persist the OTP.
    """
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
    otp = store.create_setup_otp("admin", ttl_seconds=ttl_seconds)
    return bootstrap_key, otp


def _run_server(host: str, port: int) -> None:
    """uvicorn target; no --reload, loopback-only."""
    uvicorn.run("app.main:app", host=host, port=port, reload=False, log_level="info")


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll ``(host, port)`` until it accepts a TCP connection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def launch(*, host: str = "127.0.0.1", port: int | None = None) -> int:
    """Top-level entry point used by the Briefcase bundle.

    Returns the process exit code. Keeping it importable (rather than
    inlining everything in ``main``) makes it testable.
    """
    ensure_desktop_home()
    selected_port = port if port is not None else find_free_port(host=host)
    _, otp = prepare_state_and_mint_otp()
    magic_url = f"http://{host}:{selected_port}/admin/setup-otp/{otp}"

    logger.info("desktop_launching url=%s", magic_url)
    print("EGG-API desktop launcher")
    print("------------------------")
    print("Open this URL if the window does not appear:")
    print(f"  {magic_url}")

    server_thread = threading.Thread(
        target=_run_server, args=(host, selected_port), daemon=True, name="egg-uvicorn"
    )
    server_thread.start()
    _wait_for_port(host, selected_port, timeout=5.0)

    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        print(
            "\npywebview is not available; the server stays up. Press Ctrl+C to stop.\n",
            flush=True,
        )
        # Block on the server thread: the daemon thread will die with
        # the interpreter anyway, but we want Ctrl+C to bubble up.
        with contextlib.suppress(KeyboardInterrupt):
            server_thread.join()
        return 0

    webview.create_window("EGG-API", magic_url, width=1100, height=820)
    webview.start()  # blocking — returns when the user closes the window
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return launch()
    except KeyboardInterrupt:
        return 0
    except Exception:  # pragma: no cover - defensive top-level catch
        logger.exception("desktop_launcher_failed")
        print(
            "EGG-API desktop launcher crashed — see the console log above.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
