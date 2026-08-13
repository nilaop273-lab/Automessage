from __future__ import annotations

# *storage.py — SQLite layer*
#
# Tables:
#   processed_messages — dedup guard for incoming Discord messages
#   dm_cooldowns       — per-user DM cooldown tracker
#   dm_queue           — crash-safe resume state for captcha pauses
#
# Duplicate DM fix:
#   dm_cooldowns previously only tracked the LAST dm time, so if a user
#   posted in two monitored channels quickly, both could pass is_on_cooldown()
#   before record_user_dm() was called for the first one.
#   Fix: dm_sent_log table records every DM attempt with a unique constraint
#   on (user_id, session_key) where session_key is set once per bot run.
#   is_already_dmed_this_session() gates sending before any delay fires.

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "bot_state.db"
_lock    = threading.Lock()
_conn:   sqlite3.Connection | None = None

# ── session key — unique per process run ──────────────────────────────────
# Prevents cross-session false positives while blocking same-session dupes.
_SESSION_KEY: str = str(os.getpid())


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL;")
    return _conn


def init_db() -> None:
    """Create all tables if they don't exist. Call once at startup."""
    conn = _get_connection()
    with _lock:
        # ── existing tables (unchanged) ────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id  INTEGER NOT NULL,
                kind        TEXT    NOT NULL,
                processed_at REAL   NOT NULL,
                PRIMARY KEY (message_id, kind)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_cooldowns (
                user_id     INTEGER PRIMARY KEY,
                last_dm_at  REAL    NOT NULL
            )
            """
        )
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
        # ── NEW: per-session DM dedup log ──────────────────────────────────
        # Prevents the race where two messages arrive simultaneously from
        # different monitored channels for the same user, both pass
        # is_on_cooldown() before either records the DM, and both send.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_sent_log (
                user_id     INTEGER NOT NULL,
                session_key TEXT    NOT NULL,
                sent_at     REAL    NOT NULL,
                PRIMARY KEY (user_id, session_key)
            )
            """
        )
        conn.commit()
    logger.debug("[DB] Tables initialised at %s", _DB_PATH)


# ── processed messages ─────────────────────────────────────────────────────

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


# ── dm cooldowns ───────────────────────────────────────────────────────────

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
    return (time.time() - row[0]) < cooldown_seconds


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
    logger.debug("[DB] Cooldown recorded for user %s", user_id)


# ── session-scoped DM dedup (duplicate DM fix) ─────────────────────────────

def claim_dm_slot(user_id: int) -> bool:
    """
    Atomically claim the DM slot for this user in the current session.

    Returns True  → slot claimed, safe to send
    Returns False → another coroutine already claimed it this session,
                    skip sending entirely (duplicate prevention)

    Uses INSERT OR IGNORE with a UNIQUE (user_id, session_key) constraint
    so even if two coroutines race here simultaneously, only one wins.
    """
    conn = _get_connection()
    with _lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO dm_sent_log (user_id, session_key, sent_at) VALUES (?, ?, ?)",
            (user_id, _SESSION_KEY, time.time()),
        )
        conn.commit()
        claimed = cur.rowcount == 1   # 1 = inserted (won the race), 0 = already existed

    if not claimed:
        logger.info(
            "[DB] DM slot already claimed for user %s this session — duplicate blocked",
            user_id,
        )
    return claimed


def cleanup_session_log(session_key: str | None = None) -> None:
    """Purge dm_sent_log rows for a given session (default: current session)."""
    conn = _get_connection()
    key  = session_key or _SESSION_KEY
    with _lock:
        conn.execute("DELETE FROM dm_sent_log WHERE session_key = ?", (key,))
        conn.commit()
    logger.debug("[DB] Cleared dm_sent_log for session %s", key)


# ── dm_queue helpers ───────────────────────────────────────────────────────

def queue_create(user_id: int, username: str, parts: list[str]) -> int:
    conn = _get_connection()
    with _lock:
        cur = conn.execute(
            """
            INSERT INTO dm_queue
                (user_id, username, part_index, total_parts, parts_json, status, created_at)
            VALUES (?, ?, 0, ?, ?, 'pending', ?)
            """,
            (user_id, username, len(parts), json.dumps(parts), time.time()),
        )
        conn.commit()
        row_id = cur.lastrowid
    logger.debug("[DB] dm_queue row %d created for user %s", row_id, user_id)
    return row_id  # type: ignore[return-value]


def queue_update_progress(queue_id: int, part_index: int, status: str) -> None:
    conn = _get_connection()
    with _lock:
        conn.execute(
            "UPDATE dm_queue SET part_index = ?, status = ? WHERE queue_id = ?",
            (part_index, status, queue_id),
        )
        conn.commit()


def queue_get(queue_id: int) -> dict | None:
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


# ── housekeeping ───────────────────────────────────────────────────────────

def cleanup_old_processed_messages(older_than_seconds: int = 30 * 24 * 3600) -> None:
    conn   = _get_connection()
    cutoff = time.time() - older_than_seconds
    with _lock:
        conn.execute("DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,))
        conn.commit()
    logger.debug("[DB] Pruned processed_messages older than %ds", older_than_seconds)


def queue_cleanup_done(older_than_seconds: int = 7 * 24 * 3600) -> None:
    conn   = _get_connection()
    cutoff = time.time() - older_than_seconds
    with _lock:
        conn.execute(
            "DELETE FROM dm_queue WHERE status = 'done' AND created_at < ?",
            (cutoff,),
        )
        conn.commit()
    logger.debug("[DB] Pruned done dm_queue rows older than %ds", older_than_seconds)