from __future__ import annotations

# *storage.py — SQLite layer*
# Existing tables: processed_messages, dm_cooldowns  (unchanged)
# New table:       dm_queue  — crash-safe resume state for captcha pauses
#
# dm_queue schema:
#   queue_id    — autoincrement PK
#   user_id     — who we're DMing
#   username    — display name for Telegram alerts
#   part_index  — which part (0-based) we're currently on
#   total_parts — total number of parts in the message
#   parts_json  — JSON array of all message parts (full list, not just remaining)
#   status      — 'pending' | 'paused' | 'done'
#   created_at  — unix timestamp

import json
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
        # ── new: captcha-safe queue ────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_queue (
                queue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT    NOT NULL,
                part_index  INTEGER NOT NULL DEFAULT 0,
                total_parts INTEGER NOT NULL,
                parts_json  TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                created_at  REAL    NOT NULL
            )
            """
        )
        conn.commit()


# ── existing helpers (unchanged) ───────────────────────────────────────────

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
    """Purge processed-message records older than N seconds (default 30 days)."""
    conn = _get_connection()
    cutoff = time.time() - older_than_seconds
    with _lock:
        conn.execute("DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,))
        conn.commit()


# ── new: dm_queue helpers ──────────────────────────────────────────────────

def queue_create(user_id: int, username: str, parts: list[str]) -> int:
    """
    Insert a new dm_queue row before we start sending.
    Returns the queue_id so dm_sender can update it as it progresses.
    """
    conn = _get_connection()
    with _lock:
        cur = conn.execute(
            """
            INSERT INTO dm_queue (user_id, username, part_index, total_parts, parts_json, status, created_at)
            VALUES (?, ?, 0, ?, ?, 'pending', ?)
            """,
            (user_id, username, len(parts), json.dumps(parts), time.time()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def queue_update_progress(queue_id: int, part_index: int, status: str) -> None:
    """Advance part_index and/or flip status ('pending' | 'paused' | 'done')."""
    conn = _get_connection()
    with _lock:
        conn.execute(
            "UPDATE dm_queue SET part_index = ?, status = ? WHERE queue_id = ?",
            (part_index, status, queue_id),
        )
        conn.commit()


def queue_get(queue_id: int) -> dict | None:
    """Fetch a single dm_queue row as a dict. Returns None if not found."""
    conn = _get_connection()
    with _lock:
        cur = conn.execute(
            "SELECT queue_id, user_id, username, part_index, total_parts, parts_json, status "
            "FROM dm_queue WHERE queue_id = ?",
            (queue_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "queue_id":    row[0],
        "user_id":     row[1],
        "username":    row[2],
        "part_index":  row[3],
        "total_parts": row[4],
        "parts":       json.loads(row[5]),
        "status":      row[6],
    }


def queue_cleanup_done(older_than_seconds: int = 7 * 24 * 3600) -> None:
    """Prune 'done' rows older than N seconds (default 7 days)."""
    conn = _get_connection()
    cutoff = time.time() - older_than_seconds
    with _lock:
        conn.execute(
            "DELETE FROM dm_queue WHERE status = 'done' AND created_at < ?",
            (cutoff,),
        )
        conn.commit()