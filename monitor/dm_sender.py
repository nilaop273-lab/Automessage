from __future__ import annotations

import asyncio
import itertools
import logging
import random

import discord

from monitor import storage

logger = logging.getLogger(__name__)

_rotation_cycle: itertools.cycle | None = None
_rotation_pool_id: int | None = None


def is_on_cooldown(user_id: int, cooldown_seconds: int) -> bool:
    return storage.is_user_on_cooldown(user_id, cooldown_seconds)


def record_dm(user_id: int) -> None:
    storage.record_user_dm(user_id)


def pick_message(messages: list[str], mode: str = "random") -> str:
    """Pick a message from the pool. mode: 'random' or 'sequential' (round-robin)."""
    global _rotation_cycle, _rotation_pool_id

    if not messages:
        raise ValueError("Message pool is empty")

    if len(messages) == 1:
        return messages[0]

    if mode == "random":
        return random.choice(messages)

    pool_id = id(messages)
    if _rotation_cycle is None or _rotation_pool_id != pool_id:
        _rotation_cycle = itertools.cycle(messages)
        _rotation_pool_id = pool_id
    return next(_rotation_cycle)


async def send_auto_dm(
    author: discord.User | discord.Member,
    message: str,
    delay_min: float,
    delay_max: float,
) -> bool:
    """Send a single message as one DM, after an initial delay."""
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


async def send_auto_dm_sequence(
    author: discord.User | discord.Member,
    message: str,
    delay_min: float,
    delay_max: float,
    part_delay_min: float,
    part_delay_max: float,
) -> bool:
    """Split `message` on newlines and send each line as its own DM,
    with an initial delay before the first message and a randomized
    cooldown between each subsequent part.

    Returns True only if every part was sent successfully.
    """
    parts = [line.strip() for line in message.split("\n") if line.strip()]
    if not parts:
        logger.warning("send_auto_dm_sequence called with an empty message for %s", author.id)
        return False

    delay = random.uniform(delay_min, delay_max)
    if delay > 0:
        logger.info("Waiting %.1fs before DM sequence to %s (%s)", delay, author, author.id)
        await asyncio.sleep(delay)

    for index, part in enumerate(parts, start=1):
        try:
            await author.send(part)
            logger.info(
                "Sent DM part %d/%d to %s (%s)", index, len(parts), author, author.id
            )
        except discord.Forbidden:
            logger.warning(
                "DM sequence failed for %s (%s) at part %d/%d — DMs closed or blocked",
                author, author.id, index, len(parts),
            )
            return False
        except discord.HTTPException as exc:
            logger.error(
                "DM sequence failed for %s (%s) at part %d/%d: %s",
                author, author.id, index, len(parts), exc,
            )
            return False

        if index < len(parts):
            gap = random.uniform(part_delay_min, part_delay_max)
            if gap > 0:
                logger.info(
                    "Waiting %.1fs before next DM part to %s (%s)", gap, author, author.id
                )
                await asyncio.sleep(gap)

    return True