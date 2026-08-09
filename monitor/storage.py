from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "bot_state.db"
_lock = threading.Lock()

_conn: sqlite3.Connection | None = None


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL;")
    return _conn


def init_db() -> None:
    """Create tables if they don't exist yet. Call once at startup."""
    conn = _get_connection()
    with _lock:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                processed_at REAL NOT NULL,
                PRIMARY KEY (message_id, kind)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_cooldowns (
                user_id INTEGER PRIMARY KEY,
                last_dm_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def is_message_processed(message_id: int, kind: str) -> bool:
    conn = _get_connection()
    with _lock:
        cur = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ? AND kind = ?",
            (message_id, kind),
        )
        return cur.fetchone() is not None


def mark_message_processed(message_id: int, kind: str) -> None:
    conn = _get_connection()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, kind, processed_at) VALUES (?, ?, ?)",
            (message_id, kind, time.time()),
        )
        conn.commit()


def is_user_on_cooldown(user_id: int, cooldown_seconds: int) -> bool:
    if cooldown_seconds == 0:
        return False

    conn = _get_connection()
    with _lock:
        cur = conn.execute(
            "SELECT last_dm_at FROM dm_cooldowns WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()

    if row is None:
        return False

    last_sent = row[0]
    return (time.time() - last_sent) < cooldown_seconds


def record_user_dm(user_id: int) -> None:
    conn = _get_connection()
    with _lock:
        conn.execute(
            """
            INSERT INTO dm_cooldowns (user_id, last_dm_at) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_dm_at = excluded.last_dm_at
            """,
            (user_id, time.time()),
        )
        conn.commit()


def cleanup_old_processed_messages(older_than_seconds: int = 30 * 24 * 3600) -> None:
    """Optional housekeeping: purge processed-message records older than N seconds
    (default 30 days) so the DB doesn't grow forever."""
    conn = _get_connection()
    cutoff = time.time() - older_than_seconds
    with _lock:
        conn.execute("DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,))
        conn.commit()