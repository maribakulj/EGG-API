from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.dependencies import container
from app.query_policy.engine import QueryPolicyEngine


def _build_request(url: str) -> Request:
    app = FastAPI()

    @app.get("/")
    def root(request: Request) -> dict[str, str]:
        app.state.req = request
        return {"ok": "1"}

    client = TestClient(app)
    client.get(url)
    return app.state.req


def test_query_parsing_and_filters() -> None:
    engine = QueryPolicyEngine(container.config_manager.config)
    req = _build_request("/?q=term&type=a&type=b&page=2&page_size=10&facet=type")
    nq = engine.parse(req)
    assert nq.q == "term"
    assert nq.filters["type"] == ["a", "b"]
    assert nq.page == 2


def test_page_size_enforcement() -> None:
    engine = QueryPolicyEngine(container.config_manager.config)
    req = _build_request("/?q=term&page_size=999")
    try:
        engine.parse(req)
        assert False
    except Exception as exc:
        assert "page_size exceeds" in str(exc)


def test_sort_allowlist() -> None:
    engine = QueryPolicyEngine(container.config_manager.config)
    req = _build_request("/?q=term&sort=hacker")
    try:
        engine.parse(req)
        assert False
    except Exception as exc:
        assert "Sort is not allowed" in str(exc)
