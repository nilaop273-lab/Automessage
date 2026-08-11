from __future__ import annotations

# *main.py — entry point*
# Runs the discord selfbot and the Telegram bot as two concurrent asyncio tasks
# inside the same event loop. No threads, no subprocesses — pure async.

import asyncio
import logging
import sys

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


async def _run(settings) -> None:  # type: ignore[no-untyped-def]
    # ── wire Telegram config into the notifier ─────────────────────────────
    telegram_bot.configure(settings.tg_bot_token, settings.tg_chat_id)

    # ── create the discord client ──────────────────────────────────────────
    client = create_client(settings)

    # ── launch both coroutines concurrently ───────────────────────────────
    # asyncio.gather lets both run on the same thread / event loop.
    # If the discord client crashes, the Telegram bot keeps running and vice-versa.
    tg_task = asyncio.create_task(
        telegram_bot.run_telegram_bot(settings.tg_bot_token, settings.tg_chat_id),
        name="telegram-bot",
    )

    try:
        # client.start() is the async equivalent of client.run().
        # It connects to Discord and runs the event loop until disconnected.
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