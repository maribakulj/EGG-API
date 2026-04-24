from __future__ import annotations

import contextlib
import stat
from pathlib import Path
from typing import ClassVar

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

    # Config keys that must never leave the process in cleartext. Operators
    # pin secrets via ``*_env`` sidecar references; inline values are
    # accepted in memory (for config round-trips) but get stripped or
    # masked on every externalization path.
    _REDACTED_KEYS: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("auth", "bootstrap_admin_key"),
        ("backend", "auth", "password"),
        ("backend", "auth", "token"),
    )

    # Sentinel used by the ``mask=True`` branch. Chosen for readability in
    # admin UIs; never parsed back into AppConfig (the disk form uses
    # ``mask=False`` which removes the key entirely).
    MASK_SENTINEL: ClassVar[str] = "***"

    @classmethod
    def redact(cls, data: dict[str, object], *, mask: bool = True) -> dict[str, object]:
        """Apply secret-redaction to a raw config dict.

        Two modes share one secret-key registry:

        - ``mask=True`` replaces non-empty secrets with :attr:`MASK_SENTINEL`.
          Use for **live introspection** (``GET /admin/v1/config``): admins
          see *which* secrets are configured without seeing their values.
        - ``mask=False`` removes the keys entirely. Use for **persisted
          outputs** (YAML on disk, exported config): secrets never touch
          the filesystem even at rest.

        Mutates ``data`` in place and returns it for chaining.
        """
        for path in cls._REDACTED_KEYS:
            cursor: object = data
            for part in path[:-1]:
                if not isinstance(cursor, dict) or part not in cursor:
                    cursor = None
                    break
                cursor = cursor[part]
            if not isinstance(cursor, dict):
                continue
            leaf = path[-1]
            if leaf not in cursor:
                continue
            if mask:
                if cursor[leaf]:
                    cursor[leaf] = cls.MASK_SENTINEL
            else:
                cursor.pop(leaf, None)
        return data

    def save(self, config: AppConfig) -> None:
        self._config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self.redact(config.model_dump(mode="python"), mask=False)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False))
        # The config file may contain sensitive values (backend URL with
        # embedded credentials, API-key pepper references, CORS allowlists
        # revealing partner origins). Restrict it to the owner on POSIX;
        # the chmod is a no-op on Windows, which is acceptable.
        with contextlib.suppress(OSError):
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def validate_data(self, data: dict[str, object]) -> tuple[bool, str | None]:
        try:
            AppConfig.model_validate(data)
            return True, None
        except ValidationError as exc:
            return False, str(exc)
