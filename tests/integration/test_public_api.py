from __future__ import annotations


def test_search(client) -> None:
    response = client.get("/v1/search", params={"q": "x", "facet": "type"})
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "1"


def test_get_record(client) -> None:
    response = client.get("/v1/records/abc")
    assert response.status_code == 200
    assert response.json()["id"] == "abc"


def test_facets(client) -> None:
    response = client.get("/v1/facets", params={"q": "x", "facet": "type"})
    assert response.status_code == 200
    assert "type" in response.json()["facets"]


def test_health(client) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
