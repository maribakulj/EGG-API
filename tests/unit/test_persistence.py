from __future__ import annotations

from pathlib import Path

from app.auth.api_keys import ApiKeyManager
from app.config.manager import ConfigManager
from app.storage.sqlite_store import SQLiteStore


def test_api_key_persists_across_reinstantiation(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    store1 = SQLiteStore(db_path)
    store1.initialize()
    manager1 = ApiKeyManager(store1, "bootstrap-key")
    created = manager1.create("integration-key")

    store2 = SQLiteStore(db_path)
    store2.initialize()
    manager2 = ApiKeyManager(store2, "bootstrap-key")

    assert manager2.validate(created.key) is True


def test_revocation_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    store = SQLiteStore(db_path)
    store.initialize()
    manager = ApiKeyManager(store, "bootstrap-key")
    created = manager.create("revoke-key")

    assert manager.revoke(created.key) is True

    manager_after = ApiKeyManager(SQLiteStore(db_path), "bootstrap-key")
    assert manager_after.validate(created.key) is False


def test_usage_log_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    store = SQLiteStore(db_path)
    store.initialize()
    store.log_usage_event("rid-1", "/v1/search", "GET", 200, None, "anonymous", 12, None)

    store2 = SQLiteStore(db_path)
    store2.initialize()
    summary = store2.usage_summary()

    assert summary["events"] == 1
    assert summary["errors"] == 0


def test_storage_bootstrap_creates_sqlite_file(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "state.sqlite3"
    store = SQLiteStore(db_path)
    store.initialize()
    assert db_path.exists()


def test_config_manager_path_from_env(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("backend:\n  type: elasticsearch\n")
    monkeypatch.setenv("EGG_CONFIG_PATH", str(cfg))

    manager = ConfigManager()
    assert manager.path == cfg
