"""Operator commands for a hosted server: backup, restore. Called by `sia backup` / `sia restore` (sia_devtools/cli.py).

Each returns an exit code and prints what it did; errors raise OpsError (exit 4).
"""
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx

BACKUP_NAME = re.compile(r"^sia-\d{8}-\d{6}\.db$")


class OpsError(Exception):
    pass


def integrity_check(path):
    """'ok', or SQLite's description of what is wrong."""
    try:
        with closing(sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)) as c:
            return "; ".join(r[0] for r in c.execute("PRAGMA integrity_check"))
    except sqlite3.DatabaseError as e:
        return str(e)


def copy_db(src, dest):
    """sqlite3's online backup: a consistent copy even while the server is writing (WAL mode)."""
    with closing(sqlite3.connect(src, timeout=30)) as s, closing(sqlite3.connect(dest)) as d:
        s.backup(d)


def backups(out_dir):
    """Backup files in out_dir, oldest first (the timestamped names sort by time)."""
    return sorted(p for p in Path(out_dir).glob("sia-*.db") if BACKUP_NAME.match(p.name))


def backup(db, out_dir="backups", keep=14):
    if keep < 1:
        raise OpsError("--keep must be at least 1")
    if not Path(db).is_file():
        raise OpsError(f"database {db} not found")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / time.strftime("sia-%Y%m%d-%H%M%S.db", time.gmtime())
    if dest.exists():
        raise OpsError(f"{dest} already exists; wait a second and run it again")
    try:
        copy_db(db, dest)
    except sqlite3.Error as e:
        dest.unlink(missing_ok=True)
        raise OpsError(f"backup failed: {e}")
    if (check := integrity_check(dest)) != "ok":
        dest.unlink()  # never let a bad copy count as the newest backup, or push a good one out of retention
        raise OpsError(f"integrity check failed on the copy, so it was deleted: {check}")
    old = backups(out)[:-keep]
    for p in old:
        p.unlink()
    print(f"backup {dest} ({dest.stat().st_size:,} bytes, integrity ok); {len(old)} old backup(s) deleted, "
          f"{min(keep, len(backups(out)))} kept")
    return 0


def server_reachable(base_url):
    try:
        httpx.get(base_url.rstrip("/") + "/health", timeout=3)
        return True
    except httpx.HTTPError:
        return False


def restore(db, file, base_url):
    """Replace the live database with a backup. The server must be stopped: it would keep serving from memory and
    write over the restored file."""
    if server_reachable(base_url):
        raise OpsError(f"a server answers at {base_url}: stop it first (restore never runs under a live server)")
    if (check := integrity_check(file)) != "ok":
        raise OpsError(f"{file} failed its integrity check, nothing was changed: {check}")
    safety = None
    if Path(db).is_file():
        safety = Path(db).with_name(time.strftime(f"{Path(db).stem}-before-restore-%Y%m%d-%H%M%S.db", time.gmtime()))
        copy_db(db, safety)
    copy_db(file, db)  # page-by-page copy into the existing file, so its -wal/-shm stay consistent
    print(f"restored {db} from {file}" + (f"; the previous database is saved as {safety}" if safety else ""))
    return 0


# --- CLI entry points (argparse namespaces from sia_devtools/cli.py) ---------------------------------------------
def backup_cmd(a):
    return backup(a.db, a.dir, a.keep)


def restore_cmd(a):
    return restore(a.db, a.file, os.getenv("SIA_BASE_URL", "http://127.0.0.1:8000"))
