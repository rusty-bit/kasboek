"""Nightly copy of the database into DATA_DIR/backups, keeping the newest KEEP. Uses SQLite's backup API, so
the copy is consistent even while the app writes (WAL mode). The NAS backup routine can pick the folder up."""
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path

from app import db

log = logging.getLogger("kasboek.backup")
KEEP = 14


def folder():
    return Path(db.data_dir()) / "backups"


def run(today=None, keep=KEEP):
    """Make today's backup if it doesn't exist yet. Returns its path, or None when it already existed."""
    today = today or date.today()
    out = folder() / f"kasboek-{today.isoformat()}.db"
    if out.exists():
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    src = sqlite3.connect(db.db_path())
    try:
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")   # a single self-contained file, no -wal / -shm beside it
        dst.close()
    finally:
        src.close()
    os.replace(tmp, out)
    old = sorted(folder().glob("kasboek-*.db"))[:-keep]
    for p in old:
        p.unlink()
    log.info("Backup written to %s (%s older removed)", out, len(old))
    return out
