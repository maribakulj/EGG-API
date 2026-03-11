from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/pisco.yaml")
DEFAULT_STATE_DB_PATH = Path("data/pisco_state.sqlite3")


def get_config_path() -> Path:
    return Path(os.getenv("PISCO_CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))


def get_state_db_path(config_db_path: str | None = None) -> Path:
    fallback = config_db_path or str(DEFAULT_STATE_DB_PATH)
    return Path(os.getenv("PISCO_STATE_DB_PATH", fallback))


def get_bootstrap_admin_key(config_value: str) -> str:
    return os.getenv("PISCO_BOOTSTRAP_ADMIN_KEY", config_value)
