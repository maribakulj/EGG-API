"""Sprint 21 regression tests: Windows/Mac polish without signing.

Covers the pieces that make the Briefcase bundle shippable without
an Apple Developer / Authenticode certificate:

- ``setup_file_logging`` redirects stdout + stderr + logging to
  ``{home}/logs/launcher.log`` so the windowed Windows build keeps
  its magic URL readable after launch;
- ``EGG_DESKTOP_CONSOLE=1`` disables the redirect for dev loops;
- ``show_native_dialog`` degrades gracefully when no GUI is
  available (returns False but never raises);
- the ``launch()`` pywebview-failure path invokes the native dialog
  instead of crashing silently;
- ``pyproject.toml`` carries ``console_app = false`` on the Windows
  Briefcase target and documents the first-launch page.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from app import desktop

try:
    import tomllib as _toml  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 fallback
    import tomli as _toml  # type: ignore[import-not-found]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _toml.loads((_REPO_ROOT / "pyproject.toml").read_text())


# ---------------------------------------------------------------------------
# setup_file_logging — stdio redirection
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_stdio():
    """Put back sys.stdout/stderr after the test even if the redirect
    was installed. Tests that replace them transiently must not leak
    into sibling tests."""
    saved_out, saved_err = sys.stdout, sys.stderr
    saved_handlers = list(logging.getLogger().handlers)
    saved_level = logging.getLogger().level
    try:
        yield
    finally:
        sys.stdout = saved_out
        sys.stderr = saved_err
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_setup_file_logging_creates_log_and_routes_stdio(
    tmp_path: Path, _restore_stdio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EGG_DESKTOP_CONSOLE", raising=False)
    log_path = desktop.setup_file_logging(tmp_path)

    assert log_path == tmp_path / "logs" / "launcher.log"
    assert log_path.parent.exists()

    print("magic url here")
    logging.getLogger("test").warning("something happened")

    # Flush before reading: the stream is line-buffered but the
    # explicit flush keeps the assertion race-free.
    sys.stdout.flush()  # type: ignore[attr-defined]
    contents = log_path.read_text()
    assert "magic url here" in contents
    assert "something happened" in contents


def test_setup_file_logging_respects_console_env(
    tmp_path: Path, _restore_stdio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EGG_DESKTOP_CONSOLE", "1")
    before_out = sys.stdout
    log_path = desktop.setup_file_logging(tmp_path)
    # The log dir is still created (cheap), but stdio is untouched.
    assert log_path.parent.exists()
    assert sys.stdout is before_out


def test_setup_file_logging_appends_across_runs(
    tmp_path: Path, _restore_stdio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EGG_DESKTOP_CONSOLE", raising=False)
    desktop.setup_file_logging(tmp_path)
    print("first launch")
    sys.stdout.flush()  # type: ignore[attr-defined]
    # Simulate a second launcher invocation by reopening the same
    # log file — the redirect must append, not truncate.
    desktop.setup_file_logging(tmp_path)
    print("second launch")
    sys.stdout.flush()  # type: ignore[attr-defined]
    log_path = tmp_path / "logs" / "launcher.log"
    contents = log_path.read_text()
    assert "first launch" in contents
    assert "second launch" in contents


# ---------------------------------------------------------------------------
# show_native_dialog — fallback chain
# ---------------------------------------------------------------------------


def test_show_native_dialog_never_raises_on_headless_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # We cannot guarantee an X server or tkinter in CI, so assert
    # only that the helper returns a boolean without raising.
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    result = desktop.show_native_dialog("title", "body")
    assert result in (True, False)


def test_show_native_dialog_uses_windows_api_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "win32")

    # Stub ctypes.windll so the test runs on any OS.
    calls: list[tuple[str, str]] = []

    class _FakeUser32:
        @staticmethod
        def MessageBoxW(_owner, body, title, _flags):
            calls.append((title, body))
            return 1

    class _FakeWinDLL:
        user32 = _FakeUser32()

    import types

    fake_ctypes = types.SimpleNamespace(windll=_FakeWinDLL())
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)  # type: ignore[arg-type]
    assert desktop.show_native_dialog("Hello", "World") is True
    assert calls == [("Hello", "World")]


def test_show_native_dialog_uses_osascript_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "darwin")

    executed: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        executed.append(list(cmd))
        return _FakeCompleted()

    import types

    fake_subprocess = types.SimpleNamespace(run=_fake_run)
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)  # type: ignore[arg-type]
    assert desktop.show_native_dialog("Hi", "There") is True
    assert executed and executed[0][0] == "osascript"
    assert executed[0][1] == "-e"
    # The AppleScript payload must carry both strings.
    assert "Hi" in executed[0][2]
    assert "There" in executed[0][2]


# ---------------------------------------------------------------------------
# launch() — pywebview error path invokes the native dialog
# ---------------------------------------------------------------------------


def test_launch_falls_back_to_native_dialog_when_pywebview_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EGG_HOME", str(tmp_path))
    monkeypatch.setenv("EGG_STATE_DB_PATH", str(tmp_path / "egg_state.sqlite3"))
    monkeypatch.setenv("EGG_BOOTSTRAP_ADMIN_KEY", "test-key-long-enough")
    # Avoid the real uvicorn.run().
    monkeypatch.setattr(desktop, "_run_server", lambda *a, **kw: None)
    # Force the ImportError branch: remove webview from sys.modules
    # and ensure any attempt to import it raises.
    sys.modules.pop("webview", None)
    monkeypatch.setattr(
        desktop,
        "_wait_for_port",
        lambda *a, **kw: True,  # pretend uvicorn is ready
    )

    dialog_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        desktop,
        "show_native_dialog",
        lambda title, body: dialog_calls.append((title, body)) or True,
    )

    # Simulate a non-blocking server thread join.
    import threading

    class _FakeThread:
        def start(self) -> None:
            pass

        def join(self) -> None:
            return

    monkeypatch.setattr(threading, "Thread", lambda **kw: _FakeThread())

    # Block the real pywebview import so the except path runs even
    # when the dev install has it.
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    exit_code = desktop.launch(host="127.0.0.1", port=54321)
    assert exit_code == 0
    assert dialog_calls, "native dialog should have been triggered"
    title, body = dialog_calls[0]
    assert "EGG-API" in title
    assert "http://127.0.0.1:54321/admin/setup-otp/" in body


# ---------------------------------------------------------------------------
# pyproject.toml — Briefcase Windows config
# ---------------------------------------------------------------------------


def test_briefcase_windows_is_windowed() -> None:
    win_cfg = _PYPROJECT["tool"]["briefcase"]["app"]["egg-api"]["windows"]
    assert win_cfg.get("console_app") is False, (
        "Windows Briefcase build must be windowed (console_app = false) "
        "so the MSI does not flash a cmd window at launch. S21 redirects "
        "stdio to a log file to keep the magic URL readable."
    )


def test_first_launch_doc_exists_and_references_smartscreen_and_gatekeeper() -> None:
    doc = _REPO_ROOT / "docs" / "first-launch.md"
    assert doc.exists()
    body = doc.read_text()
    for keyword in ("SmartScreen", "Gatekeeper", "WebView2", "launcher.log"):
        assert keyword in body, f"first-launch.md must mention {keyword}"
