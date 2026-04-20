from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config.models import AppConfig
from app.runtime_paths import get_config_path


class ConfigManager:
    def __init__(self, path: Path | None = None, *, require_existing: bool = False) -> None:
        self.path = path or get_config_path()
        self._config = AppConfig()

        if self.path.exists():
            self.load()
            return

        if require_existing:
            raise FileNotFoundError(
                f"Configuration file does not exist: {self.path}. Run `egg-api init` to create it."
            )

    @property
    def config(self) -> AppConfig:
        return self._config

    def load(self) -> AppConfig:
        if not self.path.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {self.path}")

        data = yaml.safe_load(self.path.read_text()) or {}
        self._config = AppConfig.model_validate(data)
        return self._config

    # Config keys that must never be serialized to the YAML file. Secrets should be
    # provided via environment variable or a 0600 sidecar file instead.
    _REDACTED_KEYS: tuple[tuple[str, ...], ...] = (("auth", "bootstrap_admin_key"),)

    @classmethod
    def _redact(cls, data: dict[str, object]) -> dict[str, object]:
        for path in cls._REDACTED_KEYS:
            cursor: object = data
            for part in path[:-1]:
                if not isinstance(cursor, dict) or part not in cursor:
                    cursor = None
                    break
                cursor = cursor[part]
            if isinstance(cursor, dict):
                cursor.pop(path[-1], None)
        return data

    def save(self, config: AppConfig) -> None:
        self._config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._redact(config.model_dump(mode="python"))
        self.path.write_text(yaml.safe_dump(data, sort_keys=False))

    def validate_data(self, data: dict[str, object]) -> tuple[bool, str | None]:
        try:
            AppConfig.model_validate(data)
            return True, None
        except ValidationError as exc:
            return False, str(exc)
