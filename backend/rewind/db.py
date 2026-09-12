"""SQLite persistence: one row per run (the whole RunReport as JSON) and one row per event.

A single shared connection guarded by a lock; every public function is safe to call from any thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .config import DB_PATH
from .models import Event, RunReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    t REAL NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_path: Path = Path(DB_PATH)


def set_path(path: Path) -> None:
    """Point the module at another database file (tests). Closes any open connection."""
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        _path = Path(path)


def _connection() -> sqlite3.Connection:
    """Open (once) and return the shared connection; callers hold `_lock`."""
    global _conn
    if _conn is None:
        _path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_path), check_same_thread=False, timeout=30)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def init_db() -> Path:
    """Create the database and tables if missing; returns the file path."""
    with _lock:
        _connection()
    return _path


def upsert_run(report: RunReport) -> None:
    with _lock:
        conn = _connection()
        conn.execute(
            "INSERT INTO runs (id, repo_url, status, created_at, report_json) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET repo_url=excluded.repo_url, status=excluded.status, "
            "created_at=excluded.created_at, report_json=excluded.report_json",
            (report.id, report.repo_url, report.status, report.created_at, report.model_dump_json()),
        )
        conn.commit()


def insert_event(run_id: str, event: Event) -> None:
    with _lock:
        conn = _connection()
        conn.execute(
            "INSERT OR REPLACE INTO events (run_id, seq, t, type, data_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, event.seq, event.t, event.type, json.dumps(event.data, ensure_ascii=False, default=str)),
        )
        conn.commit()


def list_runs() -> list[dict]:
    """Newest first: [{id, repo_url, status, created_at}]."""
    with _lock:
        rows = _connection().execute("SELECT id, repo_url, status, created_at FROM runs ORDER BY created_at DESC, rowid DESC").fetchall()
    return [{"id": r[0], "repo_url": r[1], "status": r[2], "created_at": r[3]} for r in rows]


def get_run(run_id: str) -> Optional[RunReport]:
    with _lock:
        row = _connection().execute("SELECT report_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    return RunReport.model_validate_json(row[0]) if row else None


def get_run_status(run_id: str) -> Optional[str]:
    """Just the status column: cheap enough to poll every second without parsing the report."""
    with _lock:
        row = _connection().execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row[0] if row else None


def _row_to_event(row: tuple) -> Event:
    seq, t, type_, data_json = row
    return Event(seq=seq, t=t, type=type_, data=json.loads(data_json) if data_json else {})


def get_events(run_id: str, after: int = 0) -> list[Event]:
    """Events with seq > after, in order (a range scan on the (run_id, seq) primary key)."""
    with _lock:
        rows = _connection().execute(
            "SELECT seq, t, type, data_json FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, after)
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def last_event(run_id: str) -> Optional[Event]:
    """The highest-seq event of a run, or None if it has none yet."""
    with _lock:
        row = _connection().execute(
            "SELECT seq, t, type, data_json FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
        ).fetchone()
    return _row_to_event(row) if row else None
