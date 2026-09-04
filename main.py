from __future__ import annotations

# *main.py — entry point*
#
# Two modes depending on WATCHDOG_MODE env var:
#
#   WATCHDOG_MODE=false (default — run main.py directly)
#     discord client + Telegram poll loop run together
#     no 409 conflict because watchdog is not running
#
#   WATCHDOG_MODE=true (set automatically by watchdog.py before spawning)
#     discord client runs alone — NO Telegram poll loop
#     watchdog owns the sole poll loop → fixes 409 Conflict
#     main.py still SENDS to Telegram (captcha alerts, log lines)
#     via notify_captcha() and TelegramLogHandler which use
#     one-shot aiohttp sessions, never a competing poll loop
#     main.py also runs a signal poller that reads .watchdog_signals/
#     files written by watchdog to handle /resume, /queue, /logs

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import discord

from config import load_settings
from monitor import storage
from monitor.client import create_client
from tg import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── watchdog mode ──────────────────────────────────────────────────────────
_WATCHDOG_MODE: bool = os.getenv("WATCHDOG_MODE", "false").strip().lower() == "true"

# ── signal directory (shared with watchdog.py) ─────────────────────────────
_SIGNAL_DIR = Path(__file__).resolve().parent / ".watchdog_signals"
_SIGNAL_DIR.mkdir(exist_ok=True)


# ── signal poller ──────────────────────────────────────────────────────────

async def _signal_poller(tg_token: str, tg_chat_id: int) -> None:
    """
    Polls .watchdog_signals/ every second for files written by watchdog.
    Each file name is a command. Reading + deleting it is the ack.

    Signals handled:
        resume        → captcha_queue.resume_next()
        skip:<n>      → captcha_queue.skip(n)
        queue_status  → send captcha queue snapshot to Telegram
        logs_on       → unmute TelegramLogHandler
        logs_off      → mute TelegramLogHandler
        logs_status   → send current log mute state
    """
    from tg.state import captcha_queue
    import aiohttp

    logger.info("Signal poller started — watching %s", _SIGNAL_DIR)

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(1)

            for sig_path in list(_SIGNAL_DIR.iterdir()):
                name = sig_path.name
                try:
                    sig_path.unlink()   # ack immediately — don't double-process
                except FileNotFoundError:
                    continue

                logger.info("Signal received: %s", name)

                if name == "resume":
                    await _handle_signal_resume(session, tg_token, tg_chat_id, captcha_queue)

                elif name.startswith("skip:"):
                    await _handle_signal_skip(session, tg_token, tg_chat_id, captcha_queue, name)

                elif name == "queue_status":
                    await _handle_signal_queue(session, tg_token, tg_chat_id, captcha_queue)

                elif name == "logs_off":
                    telegram_bot.set_logs_muted(True)
                    await telegram_bot.send_message(
                        session, tg_token, tg_chat_id,
                        "🔇 Log forwarding muted. Captcha alerts still active.",
                    )

                elif name == "logs_on":
                    telegram_bot.set_logs_muted(False)
                    await telegram_bot.send_message(
                        session, tg_token, tg_chat_id,
                        "🔊 Log forwarding active.",
                    )

                elif name == "logs_status":
                    state = "muted 🔇" if telegram_bot.get_logs_muted() else "active 🔊"
                    await telegram_bot.send_message(
                        session, tg_token, tg_chat_id,
                        f"Logs are currently: {state}",
                    )


async def _handle_signal_resume(session, tg_token, tg_chat_id, captcha_queue) -> None:
    from tg.state import POST_RESUME_DELAY
    import time as _time

    if not captcha_queue.is_paused:
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            "✅ No captchas pending — DMs are running normally.",
        )
        return

    snapshot    = captcha_queue.status()
    next_waiter = snapshot[0]
    remaining   = len(snapshot) - 1

    await telegram_bot.send_message(
        session, tg_token, tg_chat_id,
        (
            f"⏳ Resuming {next_waiter.username} (id: {next_waiter.user_id})\n"
            f"Part: {next_waiter.part_index + 1}/{next_waiter.total_parts}\n"
            f"Waiting {int(POST_RESUME_DELAY)}s before retrying…"
        ),
    )

    resumed = await captcha_queue.resume_next()
    if resumed is None:
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            "⚠️ Queue was empty by the time resume ran.",
        )
        return

    logger.info(
        "Resumed via signal → %s (id: %s) part %d/%d",
        resumed.username, resumed.user_id,
        resumed.part_index + 1, resumed.total_parts,
    )

    if remaining > 0:
        still = captcha_queue.status()
        lines = [f"▶️ Resumed {resumed.username} — {remaining} still pending:\n"]
        for i, w in enumerate(still, start=1):
            waited = int(_time.time() - w.queued_at)
            lines.append(
                f"{i}. {w.username} (id: {w.user_id})"
                f" — part {w.part_index + 1}/{w.total_parts}"
                f" — waiting {waited}s"
            )
        lines.append("\nSend /resume again to unblock the next one.")
        await telegram_bot.send_message(session, tg_token, tg_chat_id, "\n".join(lines))
    else:
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            f"▶️ Resumed {resumed.username} — queue empty ✅",
        )


async def _handle_signal_queue(session, tg_token, tg_chat_id, captcha_queue) -> None:
    import time as _time

    if not captcha_queue.is_paused:
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            "✅ Running normally — no captcha queue.",
        )
        return

    waiters = captcha_queue.status()
    lines   = [f"⏸ {len(waiters)} captcha(s) pending:\n"]
    for i, w in enumerate(waiters, start=1):
        waited = int(_time.time() - w.queued_at)
        lines.append(
            f"{i}. {w.username} (id: {w.user_id})\n"
            f"   Part: {w.part_index + 1}/{w.total_parts}\n"
            f"   Waiting: {waited}s\n"
            f"   Error: {w.last_error}"
        )
    lines.append("\nSend /resume to unblock the next one in queue.")
    await telegram_bot.send_message(session, tg_token, tg_chat_id, "\n".join(lines))


async def _handle_signal_skip(
    session, tg_token, tg_chat_id, captcha_queue, signal_name: str
) -> None:
    import time as _time

    # signal name format: "skip:1", "skip:2" etc
    try:
        position = int(signal_name.split(":")[1])
    except (IndexError, ValueError):
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            "❌ Invalid skip signal — expected format skip:<number>",
        )
        return

    if not captcha_queue.is_paused:
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            "✅ Queue is empty — nothing to skip.",
        )
        return

    skipped = captcha_queue.skip(position)

    if skipped is None:
        total = captcha_queue.pending
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            f"❌ Position {position} out of range — queue has {total} entry(s).\n"
            f"Send /queue to see current positions.",
        )
        return

    remaining = captcha_queue.pending
    logger.info(
        "Skipped position %d → %s (id: %s) — %d remaining",
        position, skipped.username, skipped.user_id, remaining,
    )

    if remaining > 0:
        still = captcha_queue.status()
        lines = [f"🗑 Skipped {skipped.username} (id: {skipped.user_id})\n{remaining} still pending:\n"]
        for i, w in enumerate(still, start=1):
            waited = int(_time.time() - w.queued_at)
            lines.append(
                f"{i}. {w.username} (id: {w.user_id})"
                f" — part {w.part_index + 1}/{w.total_parts}"
                f" — waiting {waited}s"
            )
        lines.append("\nSend /resume to unblock next, or /skip <n> to skip another.")
        await telegram_bot.send_message(session, tg_token, tg_chat_id, "\n".join(lines))
    else:
        await telegram_bot.send_message(
            session, tg_token, tg_chat_id,
            f"🗑 Skipped {skipped.username} (id: {skipped.user_id})\nQueue is now empty ✅",
        )


# ── run ────────────────────────────────────────────────────────────────────

async def _run(settings) -> None:  # type: ignore[no-untyped-def]
    telegram_bot.configure(settings.tg_bot_token, settings.tg_chat_id)
    client = create_client(settings)

    if _WATCHDOG_MODE:
        logger.info("WATCHDOG_MODE=true — Telegram poll loop disabled (watchdog owns it)")

        # log forwarder — drains TelegramLogHandler queue and ships batches to Telegram
        # must start here because run_telegram_bot() is skipped in WATCHDOG_MODE
        log_task = asyncio.create_task(
            telegram_bot.run_log_forwarder(settings.tg_bot_token, settings.tg_chat_id),
            name="tg-log-forwarder",
        )

        # signal poller — reads .watchdog_signals/ files written by watchdog
        signal_task = asyncio.create_task(
            _signal_poller(settings.tg_bot_token, settings.tg_chat_id),
            name="signal-poller",
        )

        try:
            await client.start(settings.token)
        except discord.LoginFailure as exc:
            logger.error("Discord login failed: %s", exc)
        finally:
            for task in (log_task, signal_task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if not client.is_closed():
                await client.close()

    else:
        logger.info("Standalone mode — running full Telegram poll loop")

        tg_task = asyncio.create_task(
            telegram_bot.run_telegram_bot(settings.tg_bot_token, settings.tg_chat_id),
            name="telegram-bot",
        )

        try:
            await client.start(settings.token)
        except discord.LoginFailure as exc:
            logger.error("Discord login failed: %s", exc)
        finally:
            tg_task.cancel()
            try:
                await tg_task
            except asyncio.CancelledError:
                pass
            if not client.is_closed():
                await client.close()


def main() -> None:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    storage.init_db()

    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()