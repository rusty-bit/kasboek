"""Running on the NAS: household login and nightly backups."""
import base64
import sqlite3
from datetime import date

from app import backup, db


def auth(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_without_login_configured_everything_is_open(client):
    assert client.get("/").status_code == 200


def test_login_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("KASBOEK_USER", "huis")
    monkeypatch.setenv("KASBOEK_PASSWORD", "correct horse")
    r = client.get("/")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/", headers=auth("huis", "wrong")).status_code == 401
    assert client.get("/", headers=auth("huis", "correct horse")).status_code == 200
    assert client.get("/health").status_code == 200                 # Docker healthcheck needs no login
    assert client.get("/eb/callback").status_code == 401


def test_backup_makes_a_consistent_copy_and_keeps_14(data_dir):
    db.init()
    with db.db() as con:
        con.execute("INSERT INTO settings(key, value) VALUES ('x', '1')")
    out = backup.run(today=date(2026, 11, 19))
    assert out.name == "kasboek-2026-11-19.db"
    c = sqlite3.connect(out)
    assert c.execute("SELECT value FROM settings WHERE key='x'").fetchone() == ("1",)
    assert c.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    c.close()
    assert backup.run(today=date(2026, 11, 19)) is None                # once a day
    for d in range(1, 20):
        backup.run(today=date(2026, 10, d))
    files = sorted(p.name for p in (data_dir / "backups").iterdir())
    assert len(files) == 14 and files[-1] == "kasboek-2026-11-19.db"
    assert "kasboek-2026-10-01.db" not in files
