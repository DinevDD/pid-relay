"""SQLite storage for local accounts and submissions."""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username   TEXT PRIMARY KEY,
  name       TEXT NOT NULL DEFAULT '',
  pw_hash    TEXT NOT NULL,
  is_admin   INTEGER NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  must_change INTEGER NOT NULL DEFAULT 1,
  created    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL,
  ns         TEXT NOT NULL,
  summary    TEXT NOT NULL,
  mode       TEXT NOT NULL,          -- 'pr' or 'direct'
  branch     TEXT,
  commit_sha TEXT,
  pr_number  INTEGER,
  pr_url     TEXT,
  state      TEXT NOT NULL DEFAULT 'submitted',
  created    REAL NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # users ---------------------------------------------------------------
    def user(self, username: str):
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE username=?", (username.lower(),)).fetchone()

    def users(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM users ORDER BY username").fetchall()

    def count_users(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username, name, password=None, is_admin=False, must_change=True) -> str:
        password = password or gen_password()
        with self._lock, self.conn() as c:
            c.execute("INSERT INTO users(username,name,pw_hash,is_admin,must_change,created) VALUES (?,?,?,?,?,?)",
                      (username.lower(), name, generate_password_hash(password), int(is_admin),
                       int(must_change), time.time()))
        return password

    def set_password(self, username, password, must_change=False):
        with self._lock, self.conn() as c:
            c.execute("UPDATE users SET pw_hash=?, must_change=? WHERE username=?",
                      (generate_password_hash(password), int(must_change), username.lower()))

    def update_user(self, username, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("name", "is_admin", "active")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self._lock, self.conn() as c:
            c.execute(f"UPDATE users SET {sets} WHERE username=?", (*allowed.values(), username.lower()))

    def authenticate(self, username, password):
        u = self.user(username or "")
        if u and u["active"] and check_password_hash(u["pw_hash"], password or ""):
            return u
        return None

    # submissions ----------------------------------------------------------
    def add_submission(self, **f) -> int:
        f.setdefault("created", time.time())
        cols = ",".join(f)
        with self._lock, self.conn() as c:
            cur = c.execute(f"INSERT INTO submissions({cols}) VALUES ({','.join('?' * len(f))})",
                            tuple(f.values()))
            return cur.lastrowid

    def update_submission(self, sid, **f):
        sets = ", ".join(f"{k}=?" for k in f)
        with self._lock, self.conn() as c:
            c.execute(f"UPDATE submissions SET {sets} WHERE id=?", (*f.values(), sid))

    def submissions(self, username=None, limit=100):
        with self.conn() as c:
            if username:
                return c.execute("SELECT * FROM submissions WHERE username=? ORDER BY id DESC LIMIT ?",
                                 (username, limit)).fetchall()
            return c.execute("SELECT * FROM submissions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def gen_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
