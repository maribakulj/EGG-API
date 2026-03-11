from __future__ import annotations


def test_validate_config(client, admin_headers) -> None:
    payload = {"backend": {"type": "elasticsearch", "url": "http://localhost:9200", "index": "records"}}
    response = client.post("/admin/v1/config/validate", json=payload, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_test_query(client, admin_headers) -> None:
    response = client.post("/admin/v1/test-query?q=hello", headers=admin_headers)
    assert response.status_code == 200
    assert "translated" in response.json()
