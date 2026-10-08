"""SQLite 报警日志存储（线程安全）"""
import sqlite3
import threading
import datetime
from app.config import DB_PATH, ensure_dirs

_lock = threading.Lock()
_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        ensure_dirs()
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                text TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reason TEXT,
                action TEXT,
                screenshot TEXT
            )"""
        )
        _conn.commit()
    return _conn


def add_event(text, verdict, reason="", action="", screenshot=""):
    with _lock:
        conn = _get_conn()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO events (ts, text, verdict, reason, action, screenshot) VALUES (?,?,?,?,?,?)",
            (ts, text, verdict, reason, action, screenshot),
        )
        conn.commit()
        return cur.lastrowid


def update_action(event_id, action):
    with _lock:
        conn = _get_conn()
        conn.execute("UPDATE events SET action=? WHERE id=?", (action, event_id))
        conn.commit()


def clear_events():
    """清空全部报警记录，返回被删除的条数"""
    with _lock:
        conn = _get_conn()
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
        conn.commit()
    return n


def list_events(limit=200):
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, ts, text, verdict, reason, action, screenshot FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows
