from __future__ import annotations

import contextlib
import os
import secrets
import stat
import sys
from pathlib import Path

DEFAULT_HOME_DIR = Path(".")
DEFAULT_CONFIG_RELATIVE = Path("config/egg.yaml")
DEFAULT_STATE_DB_RELATIVE = Path("data/egg_state.sqlite3")
DEFAULT_BOOTSTRAP_KEY_FILE = Path("data/bootstrap_admin.key")
DEFAULT_CSRF_KEY_FILE = Path("data/csrf_signing.key")

LEGACY_INSECURE_BOOTSTRAP_KEY = "admin-change-me"


def desktop_home_dir() -> Path:
    """Return the OS-native user-data directory for the desktop bundle.

    Matches what operators expect from a packaged app:

    - Windows: ``%APPDATA%\\EGG-API`` (with a safe fallback),
    - macOS:   ``~/Library/Application Support/EGG-API``,
    - Linux:   ``$XDG_DATA_HOME/egg-api`` or ``~/.local/share/egg-api``.

    The CLI flow keeps honouring the plain working directory so an
    ``egg-api init`` in a checkout still writes to ``./config`` /
    ``./data``. The Briefcase launcher sets ``EGG_HOME`` to this
    value before importing the app, which means every other module
    goes through :func:`get_home_dir` and picks the override up.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EGG-API"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "EGG-API"
        return Path.home() / "AppData" / "Roaming" / "EGG-API"
    # Linux + other POSIX (BSD, etc.)
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "egg-api"
    return Path.home() / ".local" / "share" / "egg-api"


def get_home_dir() -> Path:
    return Path(os.getenv("EGG_HOME", str(DEFAULT_HOME_DIR))).expanduser()


def get_env() -> str:
    return os.getenv("EGG_ENV", "development").strip().lower()


def is_production() -> bool:
    return get_env() == "production"


def get_config_path() -> Path:
    override = os.getenv("EGG_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return get_home_dir() / DEFAULT_CONFIG_RELATIVE


def get_state_db_path(config_db_path: str | None = None) -> Path:
    override = os.getenv("EGG_STATE_DB_PATH")
    if override:
        return Path(override).expanduser()
    if config_db_path:
        return Path(config_db_path).expanduser()
    return get_home_dir() / DEFAULT_STATE_DB_RELATIVE


def get_bootstrap_key_path() -> Path:
    override = os.getenv("EGG_BOOTSTRAP_KEY_PATH")
    if override:
        return Path(override).expanduser()
    return get_home_dir() / DEFAULT_BOOTSTRAP_KEY_FILE


def get_csrf_key_path() -> Path:
    override = os.getenv("EGG_CSRF_KEY_PATH")
    if override:
        return Path(override).expanduser()
    return get_home_dir() / DEFAULT_CSRF_KEY_FILE


def resolve_csrf_signing_key() -> bytes:
    """Return the persistent CSRF signing key, creating it if absent.

    The key lives in a 0600 sidecar under EGG_HOME so admin tokens
    survive process restarts — otherwise every deploy invalidates the
    CSRF tokens baked into every open admin tab, forcing a reload.

    Operators can pin the value via ``EGG_CSRF_SIGNING_KEY`` (hex) or
    relocate the sidecar via ``EGG_CSRF_KEY_PATH``. On a multi-node
    deploy both nodes must share the same value; the env var is the
    right affordance there.
    """
    env_value = os.getenv("EGG_CSRF_SIGNING_KEY", "").strip()
    if env_value:
        try:
            return bytes.fromhex(env_value)
        except ValueError as exc:
            raise RuntimeError(
                "EGG_CSRF_SIGNING_KEY is not valid hex (needs 64 hex chars for 32 bytes)."
            ) from exc

    sidecar = get_csrf_key_path()
    try:
        raw = sidecar.read_text().strip()
    except OSError:
        raw = ""
    if raw:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            # Corrupt sidecar — regenerate rather than crash at import.
            raw = ""

    generated = secrets.token_bytes(32)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(generated.hex())
    with contextlib.suppress(OSError):
        sidecar.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return generated


def _read_sidecar_key(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
        return value or None
    except OSError:
        return None


def _write_sidecar_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    # Best-effort on platforms that don't support chmod semantics.
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def resolve_bootstrap_admin_key(config_value: str) -> tuple[str, bool]:
    """Return (bootstrap_admin_key, was_generated).

    Precedence: env var > config value > sidecar file > auto-generate (dev only).
    In production, refuses to start if neither env nor config nor sidecar provides a key.
    Never accepts the legacy insecure default ``admin-change-me``.
    """
    env_value = os.getenv("EGG_BOOTSTRAP_ADMIN_KEY", "").strip()
    if env_value:
        if env_value == LEGACY_INSECURE_BOOTSTRAP_KEY:
            raise RuntimeError(
                "EGG_BOOTSTRAP_ADMIN_KEY is set to the insecure default 'admin-change-me'. "
                "Set it to a strong random value."
            )
        return env_value, False

    trimmed = (config_value or "").strip()
    if trimmed and trimmed != LEGACY_INSECURE_BOOTSTRAP_KEY:
        return trimmed, False

    sidecar_path = get_bootstrap_key_path()
    sidecar = _read_sidecar_key(sidecar_path)
    if sidecar and sidecar != LEGACY_INSECURE_BOOTSTRAP_KEY:
        return sidecar, False

    if is_production():
        raise RuntimeError(
            "Refusing to start in production: no bootstrap admin key provided. "
            "Set EGG_BOOTSTRAP_ADMIN_KEY or auth.bootstrap_admin_key in the config."
        )

    generated = secrets.token_urlsafe(32)
    _write_sidecar_key(sidecar_path, generated)
    return generated, True


def get_bootstrap_admin_key(config_value: str) -> str:
    """Backwards-compatible wrapper returning only the key (legacy callers)."""
    key, _ = resolve_bootstrap_admin_key(config_value)
    return key


# Env vars operators commonly use to declare a worker count.  We read all
# three because different process managers pick different conventions:
# gunicorn's ``$WEB_CONCURRENCY``, uvicorn/systemd's ``$UVICORN_WORKERS``,
# and ``$EGG_WORKERS`` as an explicit EGG-specific override.
_WORKER_ENV_VARS: tuple[str, ...] = ("EGG_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS")


def declared_worker_count() -> int:
    """Return the operator-declared worker count (best-effort; defaults to 1).

    Only honored for env vars that parse cleanly as a positive integer.
    A malformed value is treated as "unknown" rather than crashing boot.
    """
    for name in _WORKER_ENV_VARS:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return 1


def check_rate_limit_worker_safety() -> None:
    """Refuse to boot with a multi-worker config but no shared rate limiter.

    The in-memory limiter is per-process: with N workers an attacker
    effectively gets N times the published quota, making the rate limit
    advertised in the docs a lie.  Require an explicit
    ``EGG_RATE_LIMIT_REDIS_URL`` in production, warn in development.
    """
    workers = declared_worker_count()
    if workers <= 1:
        return
    if os.getenv("EGG_RATE_LIMIT_REDIS_URL", "").strip():
        return
    message = (
        f"EGG-API was started with {workers} workers but no "
        "EGG_RATE_LIMIT_REDIS_URL is configured. The in-memory rate limiter "
        "is per-process, so every worker has its own counter — the effective "
        "public limit is silently multiplied by the worker count. "
        "Either set EGG_RATE_LIMIT_REDIS_URL to a reachable Redis, or run "
        "with a single worker."
    )
    if is_production():
        raise RuntimeError(message)
    # Dev: loud warning on stderr, but don't block the CLI loop.
    import sys

    sys.stderr.write(f"[EGG-API] WARNING: {message}\n")
