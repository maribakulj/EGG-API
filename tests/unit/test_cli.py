from __future__ import annotations

import json
from pathlib import Path

from app import cli


def test_init_creates_config_and_state(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config" / "pisco.yaml"
    state = tmp_path / "state" / "pisco.sqlite3"
    monkeypatch.setenv("PISCO_CONFIG_PATH", str(config))
    monkeypatch.setenv("PISCO_STATE_DB_PATH", str(state))
    monkeypatch.setenv("PISCO_BOOTSTRAP_ADMIN_KEY", "bootstrap-test")

    parser = cli.build_parser()
    args = parser.parse_args(["init"])
    rc = args.func(args)
    assert rc == 0
    assert config.exists()
    assert state.exists()
    out = capsys.readouterr().out
    assert "Initialized state DB" in out


def test_check_config_fails_cleanly_when_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "missing.yaml"
    monkeypatch.setenv("PISCO_CONFIG_PATH", str(config))
    parser = cli.build_parser()
    args = parser.parse_args(["check-config"])
    rc = args.func(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "Configuration check failed" in err


def test_print_paths_json_output(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config" / "pisco.yaml"
    state = tmp_path / "data" / "state.sqlite3"
    monkeypatch.setenv("PISCO_CONFIG_PATH", str(config))
    monkeypatch.setenv("PISCO_STATE_DB_PATH", str(state))

    parser = cli.build_parser()
    args = parser.parse_args(["print-paths"])
    rc = args.func(args)
    assert rc == 0

    output = json.loads(capsys.readouterr().out)
    assert output["config_path"] == str(config)
    assert output["state_db_path"] == str(state)
