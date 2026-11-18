import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DB_PATH", raising=False)
    return tmp_path


@pytest.fixture
def client(data_dir):
    from app.main import app
    with TestClient(app) as c:
        yield c
