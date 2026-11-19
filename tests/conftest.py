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


@pytest.fixture(autouse=True)
def _no_background_sync(monkeypatch):
    monkeypatch.setenv("KASBOEK_SCHEDULER", "0")
    for k in ("EB_APP_ID", "EB_KEY_PATH", "EB_REDIRECT_URL", "LLM_BASE_URL", "LLM_MODEL", "KASBOEK_USER",
              "KASBOEK_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
