from __future__ import annotations

from app.dependencies import container


def test_invalid_admin_api_key(client) -> None:
    response = client.get("/admin/v1/config", headers={"x-api-key": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_forbidden_sort(client) -> None:
    response = client.get("/v1/search?q=abc&sort=bad")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "forbidden"


def test_forbidden_facet(client) -> None:
    response = client.get("/v1/search?q=abc&facet=private")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "forbidden"


def test_unknown_params_rejected(client) -> None:
    response = client.get("/v1/search?q=abc&hack=1")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_rate_limiting_behavior(client) -> None:
    container.rate_limiter.max_requests = 1
    container.rate_limiter.window_seconds = 60
    first = client.get("/v1/search?q=abc")
    second = client.get("/v1/search?q=abc")
    assert first.status_code == 200
    assert second.status_code == 429
    container.rate_limiter.max_requests = 60
