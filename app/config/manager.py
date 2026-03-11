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
                f"Configuration file does not exist: {self.path}. Run `pisco-api init` to create it."
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

    def save(self, config: AppConfig) -> None:
        self._config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(config.model_dump(mode="python"), sort_keys=False))

    def validate_data(self, data: dict[str, object]) -> tuple[bool, str | None]:
        try:
            AppConfig.model_validate(data)
            return True, None
        except ValidationError as exc:
            return False, str(exc)
