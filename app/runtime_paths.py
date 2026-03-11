from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME_DIR = Path(".")
DEFAULT_CONFIG_RELATIVE = Path("config/pisco.yaml")
DEFAULT_STATE_DB_RELATIVE = Path("data/pisco_state.sqlite3")


def get_home_dir() -> Path:
    return Path(os.getenv("PISCO_HOME", str(DEFAULT_HOME_DIR))).expanduser()


def get_config_path() -> Path:
    override = os.getenv("PISCO_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return get_home_dir() / DEFAULT_CONFIG_RELATIVE


def get_state_db_path(config_db_path: str | None = None) -> Path:
    override = os.getenv("PISCO_STATE_DB_PATH")
    if override:
        return Path(override).expanduser()
    if config_db_path:
        return Path(config_db_path).expanduser()
    return get_home_dir() / DEFAULT_STATE_DB_RELATIVE


def get_bootstrap_admin_key(config_value: str) -> str:
    return os.getenv("PISCO_BOOTSTRAP_ADMIN_KEY", config_value)
