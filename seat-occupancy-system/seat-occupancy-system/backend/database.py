"""
database.py
Small SQLite wrapper for the Seat Occupancy System.

Tables:
  users     -> login accounts for the web UI
  chairs    -> one row per tracked chair (auto-registered on first sighting)
  sessions  -> one row per "sit event": chair_id, start, end, duration_seconds
  status    -> latest known state of every chair (for the live dashboard)
"""

import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "seat_data.db")


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(default_admin_user="admin", default_admin_pass="admin123"):
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                first_seen TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chair_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration_seconds REAL,
                FOREIGN KEY (chair_id) REFERENCES chairs(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS status (
                chair_id INTEGER PRIMARY KEY,
                occupied INTEGER NOT NULL DEFAULT 0,
                since TEXT,
                last_updated TEXT NOT NULL,
                FOREIGN KEY (chair_id) REFERENCES chairs(id)
            )
        """)

        # seed a default admin account if no users exist yet
        existing = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if existing == 0:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (default_admin_user, generate_password_hash(default_admin_pass), datetime.utcnow().isoformat()),
            )


def get_or_create_chair(label):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM chairs WHERE label = ?", (label,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO chairs (label, first_seen) VALUES (?, ?)",
            (label, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def start_session(chair_id, start_time: datetime):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (chair_id, start_time) VALUES (?, ?)",
            (chair_id, start_time.isoformat()),
        )
        conn.execute(
            """INSERT INTO status (chair_id, occupied, since, last_updated)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(chair_id) DO UPDATE SET occupied=1, since=excluded.since, last_updated=excluded.last_updated""",
            (chair_id, start_time.isoformat(), start_time.isoformat()),
        )
        return cur.lastrowid


def end_session(session_id, end_time: datetime, duration_seconds: float):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET end_time = ?, duration_seconds = ? WHERE id = ?",
            (end_time.isoformat(), duration_seconds, session_id),
        )
        row = conn.execute("SELECT chair_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row:
            conn.execute(
                """INSERT INTO status (chair_id, occupied, since, last_updated)
                   VALUES (?, 0, NULL, ?)
                   ON CONFLICT(chair_id) DO UPDATE SET occupied=0, since=NULL, last_updated=excluded.last_updated""",
                (row["chair_id"], end_time.isoformat()),
            )


def touch_status(chair_id, occupied: bool, when: datetime):
    """Update last_updated without changing since/occupied (heartbeat)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO status (chair_id, occupied, since, last_updated)
               VALUES (?, ?, NULL, ?)
               ON CONFLICT(chair_id) DO UPDATE SET last_updated=excluded.last_updated""",
            (chair_id, int(occupied), when.isoformat()),
        )


def get_live_status():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.id AS chair_id, c.label, s.occupied, s.since, s.last_updated
            FROM chairs c
            LEFT JOIN status s ON s.chair_id = c.id
            ORDER BY c.id
        """).fetchall()
        return [dict(r) for r in rows]


def get_sessions(limit=200, chair_id=None):
    with get_conn() as conn:
        if chair_id:
            rows = conn.execute(
                """SELECT sess.id, sess.chair_id, c.label, sess.start_time, sess.end_time, sess.duration_seconds
                   FROM sessions sess JOIN chairs c ON c.id = sess.chair_id
                   WHERE sess.chair_id = ? AND sess.end_time IS NOT NULL
                   ORDER BY sess.start_time DESC LIMIT ?""",
                (chair_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT sess.id, sess.chair_id, c.label, sess.start_time, sess.end_time, sess.duration_seconds
                   FROM sessions sess JOIN chairs c ON c.id = sess.chair_id
                   WHERE sess.end_time IS NOT NULL
                   ORDER BY sess.start_time DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_open_session(chair_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, start_time FROM sessions WHERE chair_id = ? AND end_time IS NULL ORDER BY start_time DESC LIMIT 1",
            (chair_id,),
        ).fetchone()
        return dict(row) if row else None


def get_analytics_summary():
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(AVG(duration_seconds),0) AS avg_dur, COALESCE(SUM(duration_seconds),0) AS total_dur "
            "FROM sessions WHERE end_time IS NOT NULL"
        ).fetchone()
        by_chair = conn.execute("""
            SELECT c.label, COUNT(*) AS sessions, COALESCE(AVG(sess.duration_seconds),0) AS avg_dur,
                   COALESCE(SUM(sess.duration_seconds),0) AS total_dur
            FROM sessions sess JOIN chairs c ON c.id = sess.chair_id
            WHERE sess.end_time IS NOT NULL
            GROUP BY c.id ORDER BY total_dur DESC
        """).fetchall()
        by_day = conn.execute("""
            SELECT substr(start_time,1,10) AS day, COUNT(*) AS sessions,
                   COALESCE(SUM(duration_seconds),0) AS total_dur
            FROM sessions WHERE end_time IS NOT NULL
            GROUP BY day ORDER BY day DESC LIMIT 14
        """).fetchall()
        return {
            "total_sessions": total["c"],
            "avg_duration_seconds": total["avg_dur"],
            "total_duration_seconds": total["total_dur"],
            "by_chair": [dict(r) for r in by_chair],
            "by_day": [dict(r) for r in reversed(by_day)],
        }


def get_user(username):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
