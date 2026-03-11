from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config.models import AppConfig


class ConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        env_path = os.getenv("PISCO_CONFIG_PATH")
        self.path = path or Path(env_path or "examples/config.yaml")
        self._config = AppConfig()
        if self.path.exists():
            self.load()

    @property
    def config(self) -> AppConfig:
        return self._config

    def load(self) -> AppConfig:
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
