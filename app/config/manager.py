from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config.models import AppConfig
from app.runtime_paths import get_config_path


class ConfigManager:
    def __init__(self, path: Path | None = None, *, require_existing: bool = True) -> None:
        self.path = path or get_config_path()
        self._config = AppConfig()

        if not self.path.exists():
            if require_existing:
                raise RuntimeError(
                    f"Configuration file not found at {self.path}. Run `pisco-api init` to create defaults."
                )
            return

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

    def ensure_default_file(self) -> Path:
        if not self.path.exists():
            self.save(AppConfig())
        return self.path
