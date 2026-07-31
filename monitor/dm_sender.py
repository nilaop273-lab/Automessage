from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
logger = logging.getLogger(__name__)

_last_dm_at: dict[int, float] = {}


def is_on_cooldown(user_id: int, cooldown_seconds: int) -> bool:
    if cooldown_seconds == 0:
        return False

    last_sent = _last_dm_at.get(user_id)
    if last_sent is None:
        return False

    return (time.monotonic() - last_sent) < cooldown_seconds


def record_dm(user_id: int) -> None:
    _last_dm_at[user_id] = time.monotonic()


async def send_auto_dm(
    author: discord.User | discord.Member,
    message: str,
    delay_min: float,
    delay_max: float,
) -> bool:
    delay = random.uniform(delay_min, delay_max)
    if delay > 0:
        logger.info("Waiting %.1fs before DM to %s (%s)", delay, author, author.id)
        await asyncio.sleep(delay)

    try:
        await author.send(message)
    except discord.Forbidden:
        logger.warning(
            "DM failed for %s (%s) — DMs closed or blocked",
            author,
            author.id,
        )
        return False
    except discord.HTTPException as exc:
        logger.error("DM failed for %s (%s): %s", author, author.id, exc)
        return False

    return True
