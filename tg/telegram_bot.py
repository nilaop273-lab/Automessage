from __future__ import annotations

# *telegram_bot.py — captcha notifier + /resume listener*
# Runs as a coroutine inside the same asyncio loop as the discord selfbot.
# Uses raw aiohttp against the Telegram Bot API — no python-telegram-bot,
# no aiogram. Just POST requests. Stays in our "from scratch" mandate.

import asyncio
import logging
import time

import aiohttp

from tg.state import gate

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────
_POLL_TIMEOUT = 30          # long-poll timeout in seconds (Telegram allows up to 50)
_RETRY_SLEEP  = 5           # sleep between failed poll attempts
_BASE          = "https://api.telegram.org/bot{token}/{method}"


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
    try:
        async with session.post(
            _url(token, "sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Telegram sendMessage failed %d: %s", resp.status, body)
    except Exception as exc:
        logger.error("Telegram sendMessage exception: %s", exc)


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
        logger.error("getUpdates exception: %s", exc)
        return []


# ── main coroutine ─────────────────────────────────────────────────────────

async def run_telegram_bot(token: str, chat_id: int) -> None:
    """
    Long-poll loop. Runs forever alongside the discord client.

    Recognized commands (from your chat_id only):
        /resume  — open the pause gate, dm_sender wakes up and retries
        /status  — reply with whether the bot is currently paused
    """
    logger.info("Telegram bot started — polling for commands")

    # Drain stale updates from before this session so we don't accidentally
    # /resume a gate that was set by a previous run's leftover message.
    offset = await _drain_stale_updates(token)

    async with aiohttp.ClientSession() as session:
        while True:
            updates = await _get_updates(session, token, offset)

            if not updates:
                # Empty poll — nothing arrived in the long-poll window, loop again.
                await asyncio.sleep(0)   # yield to event loop
                continue

            for update in updates:
                offset = update["update_id"] + 1
                await _handle_update(session, token, chat_id, update)


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
            logger.warning("Failed to drain stale updates: %s: %s", type(exc).__name__, exc)
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

    else:
        await send_message(
            session, token, chat_id,
            "Unknown command.\n/resume — resume DM sending after captcha\n/status — check pause state",
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


# ── notification helper (called from dm_sender) ────────────────────────────

_tg_token: str = ""
_tg_chat_id: int = 0


def configure(token: str, chat_id: int) -> None:
    """Call once from main.py before starting the loop."""
    global _tg_token, _tg_chat_id
    _tg_token = token
    _tg_chat_id = chat_id


async def notify_captcha(username: str, user_id: int, part: int, total: int, error: str) -> None:
    """
    Send a Telegram alert to yourself when captcha fires.
    Called from dm_sender after retries are exhausted.
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