from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

DEFAULT_HOME_DIR = Path(".")
DEFAULT_CONFIG_RELATIVE = Path("config/egg.yaml")
DEFAULT_STATE_DB_RELATIVE = Path("data/egg_state.sqlite3")
DEFAULT_BOOTSTRAP_KEY_FILE = Path("data/bootstrap_admin.key")

LEGACY_INSECURE_BOOTSTRAP_KEY = "admin-change-me"


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


def _read_sidecar_key(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
        return value or None
    except OSError:
        return None


def _write_sidecar_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Best-effort on platforms that don't support chmod semantics.
        pass


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
