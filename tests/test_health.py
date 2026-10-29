from fastapi.testclient import TestClient
from app.main import app
from app.database import ping_database


def test_root():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_health():
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "healthy"


def test_ready_is_not_falsely_reported_without_database(monkeypatch):
    monkeypatch.setattr("app.routers.system.ping_database", lambda: False)
    with TestClient(app) as client:
        response = client.get("/api/v1/ready")
    assert response.status_code == 503
