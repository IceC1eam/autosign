"""SQLite 存储：被管理的北航账号、签到计划、事件日志。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  number TEXT NOT NULL UNIQUE,
  cred BLOB NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'idle',     -- idle | ok | auth_failed | captcha | error
  state_msg TEXT NOT NULL DEFAULT '',
  state_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans(
  account_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  lesson_id TEXT NOT NULL,
  name TEXT NOT NULL,
  start_ts REAL NOT NULL,
  end_ts REAL NOT NULL,
  planned_at REAL NOT NULL,
  status TEXT NOT NULL,                   -- pending | signed | failed | unknown
  attempts INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  updated REAL NOT NULL,
  PRIMARY KEY(account_id, day, lesson_id)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  account_id INTEGER,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            path = config.data_dir() / "autosign.db"
            fresh = not path.exists()
            _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            if fresh:
                os.chmod(path, 0o600)
        return _conn


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def q(sql: str, args=()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def one(sql: str, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql: str, args=()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid or 0


def event(account_id: int | None, kind: str, message: str):
    x("INSERT INTO events(ts,account_id,kind,message) VALUES(?,?,?,?)",
      (time.time(), account_id, kind, message[:300]))
    x("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 1000")
