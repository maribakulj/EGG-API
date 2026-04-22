"""Sprint 12 regression tests: deployment-surface hardening.

Covers:

- backend.auth supports basic / bearer / api_key and never leaks inline
  secrets to a round-tripped YAML config;
- proxy.allowed_hosts drives a TrustedHostMiddleware whose rejection
  surfaces a clean 400 (Starlette default) rather than a crash;
- Pydantic ``extra='forbid'`` on AppConfig rejects unknown config keys
  so silent field renames cannot happen again;
- multi-worker-without-Redis refuses to boot in production and warns
  loudly in development.
"""

from __future__ import annotations

import os

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.adapters.elasticsearch.adapter import (
    ElasticsearchAdapter,
    _build_auth_headers_and_basic,
)
from app.config.manager import ConfigManager
from app.config.models import AppConfig, BackendAuthConfig
from app.runtime_paths import check_rate_limit_worker_safety, declared_worker_count

# ---------------------------------------------------------------------------
# backend.auth
# ---------------------------------------------------------------------------


def test_backend_auth_basic_returns_httpx_tuple() -> None:
    cfg = BackendAuthConfig(mode="basic", username="u", password="p")
    headers, basic = _build_auth_headers_and_basic(cfg)
    assert headers == {}
    assert basic == ("u", "p")


def test_backend_auth_bearer_sets_authorization_header() -> None:
    cfg = BackendAuthConfig(mode="bearer", token="abc.def")
    headers, basic = _build_auth_headers_and_basic(cfg)
    assert headers == {"Authorization": "Bearer abc.def"}
    assert basic is None


def test_backend_auth_api_key_uses_es_apikey_scheme() -> None:
    cfg = BackendAuthConfig(mode="api_key", token="base64id:base64key")
    headers, _ = _build_auth_headers_and_basic(cfg)
    assert headers == {"Authorization": "ApiKey base64id:base64key"}


def test_backend_auth_none_returns_empty() -> None:
    cfg = BackendAuthConfig()
    assert _build_auth_headers_and_basic(cfg) == ({}, None)


def test_backend_auth_resolves_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_TEST_BACKEND_TOKEN", "from-env")
    cfg = BackendAuthConfig(mode="bearer", token_env="EGG_TEST_BACKEND_TOKEN")
    assert cfg.resolve_token() == "from-env"
    headers, _ = _build_auth_headers_and_basic(cfg)
    assert headers == {"Authorization": "Bearer from-env"}


def test_backend_auth_basic_requires_username() -> None:
    with pytest.raises(ValueError, match="username"):
        BackendAuthConfig(mode="basic", password="p")


def test_backend_auth_bearer_requires_token_or_token_env() -> None:
    with pytest.raises(ValueError, match="token"):
        BackendAuthConfig(mode="bearer")


def test_adapter_surfaces_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ElasticsearchAdapter(
        "http://es:9200",
        "idx",
        auth_config=BackendAuthConfig(mode="bearer", token="xyz"),
    )
    # _outgoing_headers merges tracing + auth; with no request_id bound
    # only the Authorization header is returned.
    assert adapter._outgoing_headers() == {"Authorization": "Bearer xyz"}


def test_config_manager_strips_inline_backend_secrets_on_save(tmp_path) -> None:
    path = tmp_path / "egg.yaml"
    cfg = AppConfig(
        backend={  # type: ignore[arg-type]
            "type": "elasticsearch",
            "auth": {"mode": "bearer", "token": "super-secret"},
        }
    )
    manager = ConfigManager(path, require_existing=False)
    manager.save(cfg)
    on_disk = yaml.safe_load(path.read_text())
    # The in-memory config still holds the secret (round-trips over HTTP
    # need it), but the persisted file must not.
    assert manager.config.backend.auth.token == "super-secret"
    assert "token" not in (on_disk["backend"]["auth"] or {})


# ---------------------------------------------------------------------------
# proxy.allowed_hosts + TrustedHostMiddleware
# ---------------------------------------------------------------------------


def test_trusted_host_middleware_rejects_bad_host() -> None:
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["egg.example.org"])

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    # Good host: served.
    ok = client.get("/ping", headers={"host": "egg.example.org"})
    assert ok.status_code == 200
    # Bad host: rejected before the handler runs.
    bad = client.get("/ping", headers={"host": "evil.example.com"})
    assert bad.status_code == 400


def test_proxy_allowed_hosts_exposed_on_config() -> None:
    cfg = AppConfig()
    # Default empty list; operator opts in.
    assert cfg.proxy.allowed_hosts == []
    cfg2 = AppConfig(proxy={"allowed_hosts": ["egg.example.org"]})  # type: ignore[arg-type]
    assert cfg2.proxy.allowed_hosts == ["egg.example.org"]


# ---------------------------------------------------------------------------
# Pydantic extra="forbid"
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"backendd|Extra inputs"):
        AppConfig.model_validate({"backendd": {"type": "elasticsearch"}})


def test_unknown_backend_key_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"Extra inputs|typo"):
        AppConfig.model_validate({"backend": {"type": "elasticsearch", "typo": 1}})


# ---------------------------------------------------------------------------
# Multi-worker-without-Redis guardrail
# ---------------------------------------------------------------------------


def test_declared_worker_count_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EGG_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS"):
        monkeypatch.delenv(name, raising=False)
    assert declared_worker_count() == 1
    monkeypatch.setenv("EGG_WORKERS", "4")
    assert declared_worker_count() == 4


def test_multi_worker_without_redis_raises_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EGG_WORKERS", "2")
    monkeypatch.delenv("EGG_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.setenv("EGG_ENV", "production")
    with pytest.raises(RuntimeError, match="EGG_RATE_LIMIT_REDIS_URL"):
        check_rate_limit_worker_safety()


def test_multi_worker_with_redis_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGG_WORKERS", "2")
    monkeypatch.setenv("EGG_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("EGG_ENV", "production")
    # Must not raise.
    check_rate_limit_worker_safety()


def test_multi_worker_without_redis_warns_in_dev(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EGG_WORKERS", "2")
    monkeypatch.delenv("EGG_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.setenv("EGG_ENV", "development")
    check_rate_limit_worker_safety()  # must not raise
    captured = capsys.readouterr()
    assert "EGG_RATE_LIMIT_REDIS_URL" in captured.err


def test_single_worker_is_always_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clean slate: no worker env var, no redis url → safe.
    for name in ("EGG_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EGG_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.setenv("EGG_ENV", "production")
    check_rate_limit_worker_safety()


# ---------------------------------------------------------------------------
# Smoke: app still boots unchanged when no new knob is set.
# ---------------------------------------------------------------------------


def test_default_app_boot_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EGG_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EGG_RATE_LIMIT_REDIS_URL", raising=False)
    assert os.getenv("EGG_WORKERS") is None
    # Just exercising the guard: no raise, no warning.
    check_rate_limit_worker_safety()
