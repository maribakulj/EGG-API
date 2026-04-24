"""Sprint 17 regression tests: desktop packaging helpers.

Covers the pure-Python pieces of the Briefcase bundle without actually
launching pywebview or uvicorn:

- ``desktop_home_dir`` picks the OS-native data directory;
- ``ensure_desktop_home`` honours an operator-set ``EGG_HOME`` and
  otherwise writes one;
- ``find_free_port`` returns an actually-bindable port and falls
  back when the preferred window is busy;
- ``prepare_state_and_mint_otp`` creates config/DB on first run and
  mints a usable magic-link OTP;
- ``pyproject.toml`` registers the ``egg-api-desktop`` entry point,
  the ``[desktop]`` extra and the Briefcase bundle metadata.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from app import desktop
from app.runtime_paths import desktop_home_dir

# Load pyproject.toml once so the metadata tests can share the parse.
try:
    import tomllib as _toml  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 fallback
    import tomli as _toml  # type: ignore[import-not-found]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _toml.loads((_REPO_ROOT / "pyproject.toml").read_text())


# ---------------------------------------------------------------------------
# Native paths
# ---------------------------------------------------------------------------


def test_desktop_home_dir_is_os_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    home = desktop_home_dir()
    if sys.platform == "darwin":
        assert "Library/Application Support/EGG-API" in str(home)
    elif os.name == "nt":
        assert "EGG-API" in str(home)
    else:
        # Respects XDG_DATA_HOME when set.
        monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-for-test")
        assert str(desktop_home_dir()) == "/tmp/xdg-for-test/egg-api"
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert str(desktop_home_dir()).endswith(".local/share/egg-api")
    # No exception for the outer home either.
    assert home


def test_ensure_desktop_home_honours_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EGG_HOME", str(tmp_path))
    assert desktop.ensure_desktop_home() == tmp_path


def test_ensure_desktop_home_creates_directory_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Application Support" / "EGG-API"
    monkeypatch.delenv("EGG_HOME", raising=False)
    monkeypatch.setattr(desktop, "desktop_home_dir", lambda: target)

    result = desktop.ensure_desktop_home()
    assert result == target
    assert target.exists()
    # Subsequent calls become no-ops via the now-set env var.
    assert os.environ.get("EGG_HOME") == str(target)


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------


def test_find_free_port_returns_bindable_port() -> None:
    port = desktop.find_free_port(start=18765, count=8)
    assert 18765 <= port < 18765 + 8 or port >= 1024
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_find_free_port_falls_back_when_window_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate every port in the window being taken by making bind
    # raise, which forces the fallback to the kernel-picked port.
    class _BusySocket:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def bind(self, addr):
            if addr[1] != 0:
                raise OSError("pretend port is in use")
            self._picked = 54321

        def getsockname(self):
            return ("127.0.0.1", 54321)

    monkeypatch.setattr(desktop.socket, "socket", _BusySocket)
    assert desktop.find_free_port(start=10000, count=2) == 54321


# ---------------------------------------------------------------------------
# OTP priming
# ---------------------------------------------------------------------------


def test_prepare_state_and_mint_otp_returns_usable_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin both the home and the state-db explicitly so the desktop
    # helper writes deterministically inside the tmp_path sandbox.
    monkeypatch.setenv("EGG_HOME", str(tmp_path))
    monkeypatch.setenv("EGG_STATE_DB_PATH", str(tmp_path / "egg_state.sqlite3"))
    monkeypatch.setenv("EGG_BOOTSTRAP_ADMIN_KEY", "test-key-long-enough-to-pass")

    bootstrap_key, otp = desktop.prepare_state_and_mint_otp(ttl_seconds=60)
    assert bootstrap_key == "test-key-long-enough-to-pass"
    assert otp
    # The helper must have created the DB file at the pinned path.
    assert (tmp_path / "egg_state.sqlite3").exists()
    # And the OTP must redeem through a fresh store pointed at the same file.
    from app.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "egg_state.sqlite3")
    store.initialize()
    assert store.consume_setup_otp(otp) == "admin"


# ---------------------------------------------------------------------------
# Packaging metadata
# ---------------------------------------------------------------------------


def test_desktop_extra_declared() -> None:
    extras = _PYPROJECT["project"]["optional-dependencies"]
    assert "desktop" in extras
    assert any(dep.startswith("pywebview") for dep in extras["desktop"])


def test_desktop_entry_point_declared() -> None:
    scripts = _PYPROJECT["project"]["scripts"]
    assert scripts.get("egg-api-desktop") == "app.desktop:main"


def test_briefcase_bundle_is_configured() -> None:
    tool = _PYPROJECT["tool"]["briefcase"]
    assert tool["project_name"] == "EGG-API"
    app_cfg = tool["app"]["egg-api"]
    assert app_cfg["formal_name"] == "EGG-API"
    assert "pywebview" in " ".join(app_cfg["requires"])


def test_desktop_main_is_importable() -> None:
    # launch() is exercised in integration; here we only prove the
    # module loads without pywebview installed and exposes the
    # expected symbols.
    assert callable(desktop.main)
    assert callable(desktop.launch)
    assert callable(desktop.prepare_state_and_mint_otp)
