from __future__ import annotations

# *telegram_bot.py — captcha notifier + /resume listener + log forwarder*
# Runs as a coroutine inside the same asyncio loop as the discord selfbot.
# Uses raw aiohttp against the Telegram Bot API — no python-telegram-bot,
# no aiogram. Just POST requests. Stays in our "from scratch" mandate.
#
# TelegramLogHandler attaches to the root logger so every WARNING/ERROR/INFO
# that hits the terminal also gets forwarded to your Telegram DMs.
# It uses an asyncio.Queue to bridge the sync logging.Handler.emit() call
# into the async event loop — no threads, no blocking, no dropped messages.

import asyncio
import logging
import time

import aiohttp

from tg.state import captcha_queue, POST_RESUME_DELAY

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────
_POLL_TIMEOUT  = 30
_BASE          = "https://api.telegram.org/bot{token}/{method}"
_TG_LOG_LEVEL  = logging.INFO
_TG_MAX_CHARS  = 3800
_QUEUE_MAXLEN  = 200


# ── low-level helpers ──────────────────────────────────────────────────────

def _url(token: str, method: str) -> str:
    return _BASE.format(token=token, method=method)


async def send_message(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    text: str,
) -> None:
    if len(text) > _TG_MAX_CHARS:
        text = text[:_TG_MAX_CHARS] + "\n…(truncated)"
    try:
        async with session.post(
            _url(token, "sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[TG] sendMessage failed {resp.status}: {body}")
    except Exception as exc:
        print(f"[TG] sendMessage exception: {type(exc).__name__}: {exc}")


async def _get_updates(
    session: aiohttp.ClientSession,
    token: str,
    offset: int,
) -> list[dict]:
    try:
        async with session.get(
            _url(token, "getUpdates"),
            params={"timeout": _POLL_TIMEOUT, "offset": offset},
            timeout=aiohttp.ClientTimeout(total=_POLL_TIMEOUT + 10),
        ) as resp:
            if resp.status != 200:
                logger.warning("getUpdates returned %d", resp.status)
                return []
            data = await resp.json()
            return data.get("result", [])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("getUpdates exception: %s: %s", type(exc).__name__, exc)
        return []


# ── async log queue ────────────────────────────────────────────────────────

_log_queue: asyncio.Queue[str] | None = None


def _get_log_queue() -> asyncio.Queue[str]:
    global _log_queue
    if _log_queue is None:
        _log_queue = asyncio.Queue(maxsize=_QUEUE_MAXLEN)
    return _log_queue


class TelegramLogHandler(logging.Handler):
    _BLOCKED_LOGGERS = {
        "tg.telegram_bot",
        "aiohttp.access",
        "aiohttp.client",
        "aiohttp.connector",
        "asyncio",
    }

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in self._BLOCKED_LOGGERS:
            return
        if record.name.startswith("aiohttp."):
            return
        try:
            msg = self.format(record)
            q = _get_log_queue()
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)
        except Exception:
            pass


async def run_log_forwarder(token: str, chat_id: int) -> None:
    """Public entry point — start this as an asyncio task to forward logs to Telegram.
    Called directly from main.py in WATCHDOG_MODE since run_telegram_bot() is skipped."""
    await _log_forwarder(token, chat_id)


async def _log_forwarder(token: str, chat_id: int) -> None:
    q = _get_log_queue()
    batch: list[str] = []
    BATCH_SIZE    = 10
    BATCH_TIMEOUT = 2.0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                first = await asyncio.wait_for(q.get(), timeout=BATCH_TIMEOUT)
                batch.append(first)
                while len(batch) < BATCH_SIZE:
                    try:
                        batch.append(q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                if batch:
                    await send_message(session, token, chat_id, "\n".join(batch))
                raise

            if batch:
                await send_message(session, token, chat_id, "\n".join(batch))
                batch.clear()


def attach_log_handler(token: str, chat_id: int) -> None:
    handler = TelegramLogHandler(level=_TG_LOG_LEVEL)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logger.info("Telegram log forwarding active (level: %s)", logging.getLevelName(_TG_LOG_LEVEL))


# ── resume lock — prevents two /resume being processed simultaneously ──────
_resume_in_progress: bool = False


# ── main coroutine ─────────────────────────────────────────────────────────

async def run_telegram_bot(token: str, chat_id: int) -> None:
    logger.info("Telegram bot started — polling for commands")
    offset = await _drain_stale_updates(token)

    forwarder_task = asyncio.create_task(
        _log_forwarder(token, chat_id),
        name="tg-log-forwarder",
    )

    try:
        async with aiohttp.ClientSession() as session:
            while True:
                updates = await _get_updates(session, token, offset)
                if not updates:
                    await asyncio.sleep(0)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    await _handle_update(session, token, chat_id, update)
    finally:
        forwarder_task.cancel()
        try:
            await forwarder_task
        except asyncio.CancelledError:
            pass


async def _drain_stale_updates(token: str) -> int:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                _url(token, "getUpdates"),
                params={"timeout": 0},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("result", [])
                    if results:
                        stale_offset = results[-1]["update_id"] + 1
                        logger.info(
                            "Drained %d stale Telegram update(s), starting at offset %d",
                            len(results), stale_offset,
                        )
                        return stale_offset
        except Exception as exc:
            logger.warning(
                "Failed to drain stale updates: %s: %s", type(exc).__name__, exc
            )
    return 0


async def _handle_update(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    update: dict,
) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    sender_id: int = message.get("chat", {}).get("id", -1)
    text: str = (message.get("text") or "").strip().lower()

    if sender_id != chat_id:
        logger.debug("Ignoring message from unknown chat_id %d", sender_id)
        return

    if text.startswith("/resume"):
        await _cmd_resume(session, token, chat_id)
    elif text.startswith("/skip"):
        await _cmd_skip(session, token, chat_id, text)
    elif text.startswith("/queue"):
        await _cmd_status(session, token, chat_id)
    elif text.startswith("/status"):
        await _cmd_status(session, token, chat_id)
    elif text.startswith("/logs"):
        await _cmd_logs(session, token, chat_id, text)
    elif text.startswith("/help"):
        await _cmd_help(session, token, chat_id)
    else:
        await send_message(
            session, token, chat_id,
            f"❓ Unknown command: {text}\nSend /help for the full list.",
        )


async def _cmd_resume(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
) -> None:
    global _resume_in_progress

    # Guard: don't allow two /resume to run concurrently
    if _resume_in_progress:
        await send_message(
            session, token, chat_id,
            "⏳ Resume already in progress — please wait for it to finish.",
        )
        return

    if not captcha_queue.is_paused:
        await send_message(
            session, token, chat_id,
            "✅ No captchas pending — DMs are running normally.",
        )
        return

    # Show who we're about to resume
    snapshot = captcha_queue.status()
    next_waiter = snapshot[0]
    remaining_after = len(snapshot) - 1

    await send_message(
        session, token, chat_id,
        (
            f"⏳ Resuming {next_waiter.username} (id: {next_waiter.user_id})\n"
            f"Part: {next_waiter.part_index + 1}/{next_waiter.total_parts}\n"
            f"Waiting {int(POST_RESUME_DELAY)}s before retrying to give Discord breathing room…"
        ),
    )

    _resume_in_progress = True
    try:
        resumed = await captcha_queue.resume_next()
    finally:
        _resume_in_progress = False

    if resumed is None:
        await send_message(session, token, chat_id, "⚠️ Queue was empty by the time resume ran.")
        return

    logger.info(
        "Gate opened by /resume — retrying DM to %s (user_id=%d) part %d/%d",
        resumed.username, resumed.user_id,
        resumed.part_index + 1, resumed.total_parts,
    )

    if remaining_after > 0:
        # Build a summary of who's still waiting
        still_waiting = captcha_queue.status()
        lines = [f"▶️ Resumed {resumed.username} — {remaining_after} still pending:\n"]
        for i, w in enumerate(still_waiting, start=1):
            waited = int(time.time() - w.queued_at)
            lines.append(f"{i}. {w.username} (id: {w.user_id}) — part {w.part_index + 1}/{w.total_parts} — waiting {waited}s")
        lines.append("\nSend /resume again to unblock the next one.")
        await send_message(session, token, chat_id, "\n".join(lines))
    else:
        await send_message(
            session, token, chat_id,
            f"▶️ Resumed {resumed.username} — queue empty ✅",
        )


async def _cmd_skip(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    text: str,
) -> None:
    """
    /skip <position>  — remove a waiter from the queue without resuming it.
    The DM to that user is silently cancelled.

    Usage:
        /skip 1    skip the first (oldest) entry
        /skip 2    skip the second entry
        /skip      show queue so you know what to skip
    """
    if not captcha_queue.is_paused:
        await send_message(
            session, token, chat_id,
            "✅ Queue is empty — nothing to skip.",
        )
        return

    # parse position argument
    parts = text.strip().split()
    if len(parts) < 2:
        # no position given — show queue so they can decide
        waiters = captcha_queue.status()
        lines   = [f"⏸ {len(waiters)} in queue — use /skip <number>:\n"]
        for i, w in enumerate(waiters, start=1):
            waited = int(time.time() - w.queued_at)
            lines.append(
                f"{i}. {w.username} (id: {w.user_id})"
                f" — part {w.part_index + 1}/{w.total_parts}"
                f" — waiting {waited}s"
            )
        await send_message(session, token, chat_id, "\n".join(lines))
        return

    try:
        position = int(parts[1])
    except ValueError:
        await send_message(
            session, token, chat_id,
            f"❌ Invalid position: '{parts[1]}'\nUsage: /skip 1",
        )
        return

    if position < 1:
        await send_message(
            session, token, chat_id,
            "❌ Position must be 1 or higher.",
        )
        return

    skipped = captcha_queue.skip(position)

    if skipped is None:
        total = captcha_queue.pending
        await send_message(
            session, token, chat_id,
            f"❌ Position {position} is out of range — queue has {total} entry(s).\n"
            f"Send /queue to see current positions.",
        )
        return

    remaining = captcha_queue.pending
    logger.info(
        "Skipped position %d → %s (id: %s) — %d remaining in queue",
        position, skipped.username, skipped.user_id, remaining,
    )

    if remaining > 0:
        still  = captcha_queue.status()
        lines  = [f"🗑 Skipped {skipped.username} (id: {skipped.user_id})\n"]
        lines += [f"{remaining} still pending:\n"]
        for i, w in enumerate(still, start=1):
            waited = int(time.time() - w.queued_at)
            lines.append(
                f"{i}. {w.username} (id: {w.user_id})"
                f" — part {w.part_index + 1}/{w.total_parts}"
                f" — waiting {waited}s"
            )
        lines.append("\nSend /resume to unblock next, or /skip <n> to skip another.")
        await send_message(session, token, chat_id, "\n".join(lines))
    else:
        await send_message(
            session, token, chat_id,
            f"🗑 Skipped {skipped.username} (id: {skipped.user_id})\n"
            f"Queue is now empty ✅",
        )


async def _cmd_status(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
) -> None:
    if not captcha_queue.is_paused:
        await send_message(
            session, token, chat_id,
            "✅ Running normally — no captcha queue.",
        )
        return

    waiters = captcha_queue.status()
    lines = [f"⏸ {len(waiters)} captcha(s) pending:\n"]
    for i, w in enumerate(waiters, start=1):
        waited = int(time.time() - w.queued_at)
        lines.append(
            f"{i}. {w.username} (id: {w.user_id})\n"
            f"   Part: {w.part_index + 1}/{w.total_parts}\n"
            f"   Waiting: {waited}s\n"
            f"   Error: {w.last_error}"
        )
    lines.append("\nSend /resume to unblock the next one in queue.")
    await send_message(session, token, chat_id, "\n".join(lines))


# mute flag — toggled by /logs on|off or watchdog signal
_logs_muted: bool = False


def set_logs_muted(value: bool) -> None:
    """Called by main.py signal poller when watchdog forwards /logs on|off."""
    global _logs_muted
    _logs_muted = value


def get_logs_muted() -> bool:
    return _logs_muted


async def _cmd_help(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
) -> None:
    await send_message(
        session, token, chat_id,
        (
            "📖  COMMAND LIST\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "⚠️  CAPTCHA\n"
            "/resume      unblock next stuck DM\n"
            "/skip <n>    cancel DM at queue position n\n"
            "/queue       show full captcha queue\n"
            "\n"
            "📋  LOGS\n"
            "/logs on   enable log forwarding\n"
            "/logs off  mute log forwarding\n"
            "/logs      check current log state\n"
            "\n"
            "❓  OTHER\n"
            "/help      show this message\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡  captcha alerts always fire even with /logs off\n"
            "💡  use /start /stop /status via watchdog"
        ),
    )


async def _cmd_logs(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    text: str,
) -> None:
    global _logs_muted
    if "off" in text:
        _logs_muted = True
        await send_message(session, token, chat_id, "🔇 Log forwarding muted. Captcha alerts still active.")
    elif "on" in text:
        _logs_muted = False
        await send_message(session, token, chat_id, "🔊 Log forwarding active.")
    else:
        state = "muted 🔇" if _logs_muted else "active 🔊"
        await send_message(session, token, chat_id, f"Logs are currently: {state}")


# ── notification helper (called from dm_sender) ────────────────────────────

_tg_token:   str = ""
_tg_chat_id: int = 0


def configure(token: str, chat_id: int) -> None:
    global _tg_token, _tg_chat_id
    _tg_token  = token
    _tg_chat_id = chat_id
    attach_log_handler(token, chat_id)


async def notify_captcha(
    username: str,
    user_id: int,
    part: int,
    total: int,
    error: str,
    queue_size: int = 1,
) -> None:
    """
    Immediate captcha alert — bypasses log queue, fires its own aiohttp session.
    Always sends regardless of _logs_muted.
    queue_size = how many are now pending (including this one).
    """
    if not _tg_token or not _tg_chat_id:
        logger.warning("Telegram not configured — captcha alert skipped")
        return

    text = (
        f"⚠️ CAPTCHA HIT ({queue_size} in queue)\n"
        f"User: {username} (id: {user_id})\n"
        f"Part: {part}/{total}\n"
        f"Error: {error}\n\n"
        f"Go solve the captcha in Discord mobile, then send /resume here."
    )
    if queue_size > 1:
        text += f"\n\n📋 {queue_size - 1} other captcha(s) already waiting — each /resume handles one."

    async with aiohttp.ClientSession() as session:
        await send_message(session, _tg_token, _tg_chat_id, text)