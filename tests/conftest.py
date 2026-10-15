import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import db, main, mock
from app.providers import Providers


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_KEY", "test-service-key")
    monkeypatch.setenv("RETRY_DELAY_SCALE", "0")
    # .env.example deliberately has blank live credentials. Mocks must still work.
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "")
    monkeypatch.setattr(db, "engine", create_engine("sqlite:///" + str(tmp_path / "app.db")))
    monkeypatch.setattr(mock, "engine", create_engine("sqlite:///" + str(tmp_path / "mock.db")))
    providers = Providers()
    monkeypatch.setattr(main, "providers", providers)
    with TestClient(mock.app) as remote, TestClient(main.app) as client:
        providers.client = remote
        client.headers["X-Service-Key"] = "test-service-key"
        yield client, remote
