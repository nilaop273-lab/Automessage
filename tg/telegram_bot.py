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
from collections import deque

import aiohttp

from tg.state import gate

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────
_POLL_TIMEOUT = 30          # long-poll timeout in seconds (Telegram allows up to 50)
_RETRY_SLEEP  = 5           # sleep between failed poll attempts
_BASE          = "https://api.telegram.org/bot{token}/{method}"

# Log levels that get forwarded to Telegram.
# DEBUG is excluded — too noisy. Change to logging.DEBUG if you want everything.
_TG_LOG_LEVEL = logging.INFO

# Max characters per Telegram message (Telegram hard cap is 4096).
_TG_MAX_CHARS = 3800

# How many log messages can queue up before we start dropping oldest ones.
# Prevents unbounded memory growth if Telegram is slow.
_QUEUE_MAXLEN = 200


# ── low-level helpers ──────────────────────────────────────────────────────

def _url(token: str, method: str) -> str:
    return _BASE.format(token=token, method=method)


async def send_message(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    text: str,
) -> None:
    """Fire-and-forget a Telegram message. Logs on failure, never raises."""
    # Truncate if over Telegram's limit
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
                # Use print here to avoid infinite loop (logger → handler → send_message → logger)
                print(f"[TG] sendMessage failed {resp.status}: {body}")
    except Exception as exc:
        print(f"[TG] sendMessage exception: {type(exc).__name__}: {exc}")


async def _get_updates(
    session: aiohttp.ClientSession,
    token: str,
    offset: int,
) -> list[dict]:
    """Long-poll /getUpdates. Returns list of update dicts (may be empty)."""
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
# The sync Handler.emit() drops records into this queue.
# The async _log_forwarder() coroutine drains it and sends to Telegram.

_log_queue: asyncio.Queue[str] | None = None


def _get_log_queue() -> asyncio.Queue[str]:
    global _log_queue
    if _log_queue is None:
        _log_queue = asyncio.Queue(maxsize=_QUEUE_MAXLEN)
    return _log_queue


class TelegramLogHandler(logging.Handler):
    """
    Sync logging.Handler that enqueues formatted log records.
    Safe to call from any thread — uses Queue.put_nowait() which
    never blocks. If the queue is full the oldest entry is dropped
    and the new one takes its place.
    """

    # Loggers whose records we never forward — avoids infinite loops
    # and stops aiohttp's own debug spam from flooding Telegram.
    _BLOCKED_LOGGERS = {
        "tg.telegram_bot",      # our own module — would cause send → log → send loop
        "aiohttp.access",
        "aiohttp.client",
        "aiohttp.connector",
        "asyncio",
    }

    def emit(self, record: logging.LogRecord) -> None:
        # Block noisy / recursive loggers
        if record.name in self._BLOCKED_LOGGERS:
            return
        if record.name.startswith("aiohttp."):
            return

        try:
            msg = self.format(record)
            q = _get_log_queue()
            if q.full():
                # Drop oldest to make room — deque trick via get_nowait
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)
        except Exception:
            # Never let the handler crash the bot
            pass


async def _log_forwarder(token: str, chat_id: int) -> None:
    """
    Drains the log queue and ships each record to Telegram.
    Runs as a background task inside run_telegram_bot().
    Batches rapid-fire logs into one message (up to 10 lines or 2s gap)
    so Telegram's rate limit (30 msg/s) is never touched.
    """
    q = _get_log_queue()
    batch: list[str] = []
    BATCH_SIZE = 10
    BATCH_TIMEOUT = 2.0          # seconds to wait before flushing a partial batch

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Block until first item arrives
                first = await asyncio.wait_for(q.get(), timeout=BATCH_TIMEOUT)
                batch.append(first)

                # Drain any more that arrived at the same time (non-blocking)
                while len(batch) < BATCH_SIZE:
                    try:
                        batch.append(q.get_nowait())
                    except asyncio.QueueEmpty:
                        break

            except asyncio.TimeoutError:
                pass  # no new logs in timeout window, flush what we have
            except asyncio.CancelledError:
                # Flush remaining before exit
                if batch:
                    await send_message(session, token, chat_id, "\n".join(batch))
                raise

            if batch:
                await send_message(session, token, chat_id, "\n".join(batch))
                batch.clear()


def attach_log_handler(token: str, chat_id: int) -> None:
    """
    Attach TelegramLogHandler to the root logger.
    Call once from configure() — after this every log at INFO+ goes to Telegram.
    """
    handler = TelegramLogHandler(level=_TG_LOG_LEVEL)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logger.info("Telegram log forwarding active (level: %s)", logging.getLevelName(_TG_LOG_LEVEL))


# ── main coroutine ─────────────────────────────────────────────────────────

async def run_telegram_bot(token: str, chat_id: int) -> None:
    """
    Long-poll loop. Runs forever alongside the discord client.

    Recognized commands (from your chat_id only):
        /resume  — open the pause gate, dm_sender wakes up and retries
        /status  — reply with whether the bot is currently paused
        /logs on — enable log forwarding (default: already on)
        /logs off — mute log forwarding (captcha alerts still come through)
    """
    logger.info("Telegram bot started — polling for commands")

    # Drain stale updates from before this session so we don't accidentally
    # /resume a gate that was set by a previous run's leftover message.
    offset = await _drain_stale_updates(token)

    # Start the log forwarder as a sibling task
    forwarder_task = asyncio.create_task(
        _log_forwarder(token, chat_id),
        name="tg-log-forwarder",
    )

    try:
        async with aiohttp.ClientSession() as session:
            while True:
                updates = await _get_updates(session, token, offset)

                if not updates:
                    await asyncio.sleep(0)   # yield to event loop
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
    """
    Fetch all pending updates with timeout=0 to get current offset,
    then discard them. Prevents old /resume commands from firing on startup.
    """
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
    """Route a single Telegram update to the right handler."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    sender_id: int = message.get("chat", {}).get("id", -1)
    text: str = (message.get("text") or "").strip().lower()

    # Only accept commands from YOUR chat_id — ignore everything else silently.
    if sender_id != chat_id:
        logger.debug("Ignoring message from unknown chat_id %d", sender_id)
        return

    if text.startswith("/resume"):
        await _cmd_resume(session, token, chat_id)

    elif text.startswith("/status"):
        await _cmd_status(session, token, chat_id)

    elif text.startswith("/logs"):
        await _cmd_logs(session, token, chat_id, text)

    else:
        await send_message(
            session, token, chat_id,
            (
                "Unknown command.\n\n"
                "/resume — resume DM sending after captcha\n"
                "/status — check pause state\n"
                "/logs on — enable log forwarding\n"
                "/logs off — mute log forwarding"
            ),
        )


async def _cmd_resume(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
) -> None:
    if not gate.is_paused:
        await send_message(
            session, token, chat_id,
            "✅ Bot is not paused — DMs are already running.",
        )
        return

    ctx = gate.context
    gate.resume()
    logger.info(
        "Gate opened by /resume — retrying DM to %s (user_id=%d) from part %d/%d",
        ctx.username, ctx.user_id, ctx.part_index + 1, ctx.total_parts,
    )
    await send_message(
        session, token, chat_id,
        (
            f"▶️ Resuming DM to {ctx.username} (id: {ctx.user_id})\n"
            f"Retrying from part {ctx.part_index + 1}/{ctx.total_parts}"
        ),
    )


async def _cmd_status(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
) -> None:
    if gate.is_paused:
        ctx = gate.context
        await send_message(
            session, token, chat_id,
            (
                f"⏸ PAUSED\n"
                f"Stuck on: {ctx.username} (id: {ctx.user_id})\n"
                f"Part: {ctx.part_index + 1}/{ctx.total_parts}\n"
                f"Error: {ctx.last_error}"
            ),
        )
    else:
        await send_message(
            session, token, chat_id,
            "✅ Running normally — no captcha pause active.",
        )


# mute flag — toggled by /logs on|off
_logs_muted: bool = False


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

_tg_token: str = ""
_tg_chat_id: int = 0


def configure(token: str, chat_id: int) -> None:
    """Call once from main.py before starting the loop."""
    global _tg_token, _tg_chat_id
    _tg_token = token
    _tg_chat_id = chat_id
    attach_log_handler(token, chat_id)


async def notify_captcha(username: str, user_id: int, part: int, total: int, error: str) -> None:
    """
    Send a Telegram alert to yourself when captcha fires.
    Called from dm_sender after retries are exhausted.
    Always sends regardless of _logs_muted.
    """
    if not _tg_token or not _tg_chat_id:
        logger.warning("Telegram not configured — captcha alert skipped")
        return

    text = (
        f"⚠️ CAPTCHA HIT\n"
        f"User: {username} (id: {user_id})\n"
        f"Part: {part}/{total}\n"
        f"Error: {error}\n\n"
        f"Go solve the captcha in Discord mobile, then send /resume here."
    )

    async with aiohttp.ClientSession() as session:
        await send_message(session, _tg_token, _tg_chat_id, text)