from __future__ import annotations


def test_validate_config(client, admin_headers) -> None:
    payload = {
        "backend": {"type": "elasticsearch", "url": "http://localhost:9200", "index": "records"},
        "storage": {"sqlite_path": "data/pisco_state.sqlite3"},
    }
    response = client.post("/admin/v1/config/validate", json=payload, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_test_query(client, admin_headers) -> None:
    response = client.post("/admin/v1/test-query?q=hello", headers=admin_headers)
    assert response.status_code == 200
    assert "translated" in response.json()


def test_admin_config_exposes_paths(client, admin_headers) -> None:
    response = client.get("/admin/v1/config", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()
    assert "config_path" in payload
    assert "state_db_path" in payload
    assert "config" in payload


def test_admin_status_includes_usage_summary(client, admin_headers) -> None:
    client.get("/v1/search?q=hello")
    response = client.get("/admin/v1/status", headers=admin_headers)
    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage["events"] >= 1
