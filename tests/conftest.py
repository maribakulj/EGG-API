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

# Env vars that a test may monkey-patch in isolation but that leak into
# the next test's container rebuild if not wiped. Listed explicitly so
# the autouse fixture has a complete teardown contract — adding a new
# env-var knob means adding it here too.
_LEAKY_ENV_VARS = (
    "EGG_OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "EGG_METRICS_TOKEN",
    "EGG_RATE_LIMIT_REDIS_URL",
    "EGG_API_KEY_PEPPER",
    "EGG_CSRF_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def reset_container(tmp_path, monkeypatch) -> None:
    # Scrub optional-feature env vars so a test that set one does not
    # contaminate the container rebuild performed by the next test.
    for var in _LEAKY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Reset the tracing singleton. Pre-Sprint-10 this was only possible
    # via ``sys.modules.pop("app.tracing")``, which re-ran every module
    # side-effect and leaked state to later tests in the same worker.
    from app.tracing import reset_for_tests as reset_tracing

    reset_tracing()

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
