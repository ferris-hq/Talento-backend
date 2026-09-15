from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_without_database_is_unavailable(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["database"] == "not configured"
