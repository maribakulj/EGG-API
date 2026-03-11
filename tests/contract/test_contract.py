from __future__ import annotations

from app.schemas.record import Record


def test_openapi_route(client) -> None:
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    assert "paths" in response.json()


def test_response_model_validates(client) -> None:
    response = client.get("/v1/records/1")
    assert response.status_code == 200
    record = Record.model_validate(response.json())
    assert record.id == "1"
