"""Running on the NAS: nightly backups. The login is tested in test_login.py."""
import sqlite3
from datetime import date

from app import backup, db


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


def test_compose_files_use_the_same_published_image():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    tags = {f: re.findall(r"image:\s*(\S+)", (root / f).read_text(encoding="utf-8"))
            for f in ("docker-compose.yml", "portainer-stack.yml")}
    assert tags["docker-compose.yml"] == tags["portainer-stack.yml"]
    assert re.fullmatch(r"ghcr\.io/[\w-]+/kasboek:\d+\.\d+\.\d+", tags["docker-compose.yml"][0])   # pinned, no :latest
