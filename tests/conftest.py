from __future__ import annotations

import os
import tempfile

# Pin env vars before any app modules load so the container doesn't
# generate a sidecar file under the repo working tree.
os.environ.setdefault("EGG_BOOTSTRAP_ADMIN_KEY", "test-admin-key-abcdefghijklmnop")
os.environ.setdefault("EGG_HOME", tempfile.mkdtemp(prefix="egg-test-home-"))

import pytest
from fastapi.testclient import TestClient

from app.auth.api_keys import ApiKeyManager
from app.config.models import AppConfig
from app.dependencies import container
from app.mappers.schema_mapper import SchemaMapper
from app.query_policy.engine import QueryPolicyEngine
from app.rate_limit.limiter import InMemoryRateLimiter
from app.storage.sqlite_store import SQLiteStore
from tests._fakes import FakeAdapter


@pytest.fixture(autouse=True)
def reset_container(tmp_path) -> None:
    container.adapter = FakeAdapter()
    container.rate_limiter = InMemoryRateLimiter()
    container.login_rate_limiter = InMemoryRateLimiter(max_requests=1000, window_seconds=60)
    cfg = AppConfig()
    # TestClient talks http://, so secure cookies would never round-trip.
    cfg.auth.admin_cookie_secure = False
    cfg.auth.admin_cookie_samesite = "lax"
    # Deterministic admin key for tests without requiring a sidecar file.
    cfg.auth.bootstrap_admin_key = "test-admin-key-abcdefghijklmnop"
    container.config_manager._config = cfg
    container.store = SQLiteStore(tmp_path / "state.sqlite3")
    container.store.initialize()
    container.api_keys = ApiKeyManager(container.store, cfg.auth.bootstrap_admin_key)
    container.mapper = SchemaMapper(cfg)
    container.policy = QueryPolicyEngine(cfg)
    yield


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"x-api-key": container.api_keys.default_admin_key}


@pytest.fixture()
def admin_ui_session(client: TestClient, admin_headers: dict[str, str]) -> str:
    """Log in through the admin UI and return the CSRF token for POSTs.

    All state-changing UI handlers require ``csrf_token`` (form field or
    ``X-CSRF-Token`` header). Tests that exercise those endpoints should
    submit the returned value.
    """
    from app.admin_ui.auth import _csrf_for_session

    response = client.post(
        "/admin/login",
        data={"api_key": admin_headers["x-api-key"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session_token = client.cookies.get("egg_admin_session")
    assert session_token, "login should set the session cookie"
    return _csrf_for_session(session_token)
