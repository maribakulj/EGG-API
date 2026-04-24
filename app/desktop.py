"""Desktop launcher (Sprint 17, hardened in Sprint 21).

Entry point for the Briefcase-packaged EGG-API desktop app. Runs
uvicorn in a daemon thread, primes the config + state DB on first
launch, mints a magic link, and opens it inside a pywebview window.

Sprint 21 polish:
- redirects stdout/stderr to ``EGG_HOME/logs/launcher.log`` at the
  very start so a windowed (non-console) Briefcase build does not
  lose the magic URL or uvicorn output;
- falls back to a platform-native ``MessageBox`` / AppleScript
  dialog when pywebview cannot open (missing WebView2 runtime on
  old Windows, missing WebKit on headless Linux), so the operator
  still sees the URL instead of a silent crash.

No ``import webview`` at module scope: lets ``app.desktop`` be
imported during tests on hosts that don't have pywebview installed.
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


def setup_file_logging(home: Path) -> Path:
    """Redirect stdout + stderr + logging to ``{home}/logs/launcher.log``.

    Windowed Briefcase builds (``console_app = false``) have no
    console attached: without this the magic URL the CLI prints
    evaporates into ``/dev/null``-equivalent. We append to a file
    under ``EGG_HOME`` instead, which doubles as a post-mortem log
    for the support team.

    Called unconditionally at the top of :func:`main`. No-op when
    ``EGG_DESKTOP_CONSOLE=1`` so developers iterating locally keep
    the usual terminal output.
    """
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launcher.log"

    if os.environ.get("EGG_DESKTOP_CONSOLE", "").strip().lower() in {"1", "true", "yes"}:
        return log_path

    # Open in append so consecutive launches keep history; line-buffered
    # so the file is usable from a text editor while the app is running.
    # NOTE: intentionally not using a context manager — the stream needs
    # to outlive this function and persist for the whole process.
    stream = open(  # noqa: SIM115
        log_path, "a", buffering=1, encoding="utf-8", errors="replace"
    )
    sys.stdout = stream
    sys.stderr = stream

    # Rebind Python logging to the same stream so uvicorn logs + our
    # ``logger.info()`` calls all land in the single file.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    return log_path


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


def show_native_dialog(title: str, body: str) -> bool:
    """Best-effort native message box with ``title`` + ``body``.

    Windows: uses ``user32.MessageBoxW`` via ctypes (no extra dep,
    bundled WinAPI).  macOS: uses ``osascript`` to display a dialog.
    Linux/other: falls back to ``tkinter.messagebox`` when available.
    Returns ``True`` when something visible was shown, ``False``
    otherwise — callers can decide whether to also write to the log.
    """
    # Keep imports platform-specific so an unavailable backend never
    # breaks the launcher on the other OSes.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, body, title, 0x00000040)  # MB_ICONINFORMATION
            return True
        except Exception:
            logger.exception("native_dialog_failed_windows")
            return False
    if sys.platform == "darwin":
        try:
            import shlex
            import subprocess

            script = (
                f"display dialog {shlex.quote(body)} with title {shlex.quote(title)} "
                'buttons {"OK"} default button "OK"'
            )
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)  # noqa: S603, S607
            return True
        except Exception:
            logger.exception("native_dialog_failed_macos")
            return False
    # Linux / others — best effort via tkinter. Many Briefcase Linux
    # bundles ship tkinter already (std library). If it's missing,
    # the caller still has the log file + stdout fallback.
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showinfo(title, body)
        root.destroy()
        return True
    except Exception:
        logger.exception("native_dialog_failed_linux")
        return False


def launch(*, host: str = "127.0.0.1", port: int | None = None) -> int:
    """Top-level entry point used by the Briefcase bundle.

    Returns the process exit code. Keeping it importable (rather than
    inlining everything in ``main``) makes it testable.
    """
    home = ensure_desktop_home()
    selected_port = port if port is not None else find_free_port(host=host)
    _, otp = prepare_state_and_mint_otp()
    magic_url = f"http://{host}:{selected_port}/admin/setup-otp/{otp}"

    logger.info("desktop_launching url=%s", magic_url)
    print("EGG-API desktop launcher")
    print("------------------------")
    print(f"Home directory: {home}")
    print("Open this URL if the window does not appear:")
    print(f"  {magic_url}")

    server_thread = threading.Thread(
        target=_run_server, args=(host, selected_port), daemon=True, name="egg-uvicorn"
    )
    server_thread.start()
    _wait_for_port(host, selected_port, timeout=5.0)

    try:
        import webview
    except ImportError:
        logger.warning("pywebview_unavailable; falling back to native dialog")
        # Surface the URL through whatever the OS can show natively;
        # keep the server running so the operator can paste into a
        # browser if the dialog is dismissed.
        show_native_dialog(
            "EGG-API",
            (
                "EGG-API is running but the embedded window is unavailable.\n\n"
                f"Open this URL in your browser to finish setup:\n{magic_url}"
            ),
        )
        with contextlib.suppress(KeyboardInterrupt):
            server_thread.join()
        return 0

    try:
        webview.create_window("EGG-API", magic_url, width=1100, height=820)
        webview.start()  # blocking — returns when the user closes the window
    except Exception:
        # WebView2 runtime missing on Windows 10 <2004, WebKitGTK
        # missing on headless Linux, etc. Keep the server up and
        # show the URL in a native dialog so the operator always
        # has a way forward.
        logger.exception("pywebview_start_failed; falling back to native dialog")
        show_native_dialog(
            "EGG-API — embedded window failed to open",
            (
                "EGG-API is running but the embedded window failed to start "
                "(usually a missing WebView2 runtime on Windows 10, or "
                "missing WebKitGTK on Linux).\n\n"
                f"Open this URL in your browser to finish setup:\n{magic_url}"
            ),
        )
        with contextlib.suppress(KeyboardInterrupt):
            server_thread.join()
    return 0


def main() -> int:
    # S21: the Briefcase Windows build runs windowed (console_app =
    # false), so attached-console output is not available. Redirect
    # everything to a file under EGG_HOME **before** any print() or
    # logging call so we never lose the magic URL.
    home = ensure_desktop_home()
    # If the log file cannot be opened (weird FS perms) keep the
    # original stdio; the launcher still works, logs just leak.
    with contextlib.suppress(Exception):
        setup_file_logging(home)
    try:
        return launch()
    except KeyboardInterrupt:
        return 0
    except Exception:  # pragma: no cover - defensive top-level catch
        logger.exception("desktop_launcher_failed")
        show_native_dialog(
            "EGG-API — launcher crashed",
            (
                "The EGG-API launcher crashed during startup.\n"
                f"Check the log at: {home}/logs/launcher.log"
            ),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
