from __future__ import annotations

# *dm_sender.py — DM delivery engine with captcha-safe pause/resume*
#
# Flow for send_auto_dm_sequence:
#   1. Split message into parts, persist the full list to dm_queue (crash-safe)
#   2. For each part:
#       a. Check the pause gate — block here if we're already paused
#       b. Send the part
#       c. On success  → advance queue row, inter-part delay, next part
#       d. On captcha  → immediately pause gate + notify Telegram + await /resume
#                        → retry the same part once after resume
#       e. On other HTTP error or Forbidden → log and return False immediately
#   3. On full success → mark queue 'done', return True

import asyncio
import itertools
import logging
import random

import discord

from monitor import storage
from tg import telegram_bot
from tg.state import gate, PauseContext

logger = logging.getLogger(__name__)

# ── rotation state (module-level, same as original) ───────────────────────
_rotation_cycle: itertools.cycle | None = None
_rotation_pool_id: int | None = None


# ── helpers (unchanged API) ───────────────────────────────────────────────

def is_on_cooldown(user_id: int, cooldown_seconds: int) -> bool:
    return storage.is_user_on_cooldown(user_id, cooldown_seconds)


def record_dm(user_id: int) -> None:
    storage.record_user_dm(user_id)


def pick_message(messages: list[str], mode: str = "random") -> str:
    """Pick a message from the pool. mode: 'random' or 'sequential'."""
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


# ── captcha fingerprinting ────────────────────────────────────────────────

def _is_captcha_error(exc: discord.HTTPException) -> bool:
    """
    Identify captcha errors from Discord's HTTP response.

    From your log:
        400 Bad Request (error code: -1): Captcha required
    Discord also sends JSON with "captcha_key" in the body for some flows.
    We catch both.
    """
    err_text = str(exc).lower()
    return (
        "captcha" in err_text
        or getattr(exc, "code", None) == -1
        or exc.status == 400 and "captcha" in err_text
    )


# ── single-message send (unchanged public API) ────────────────────────────

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
            author, author.id,
        )
        return False
    except discord.HTTPException as exc:
        logger.error("DM failed for %s (%s): %s", author, author.id, exc)
        return False

    return True


# ── core: captcha-aware sequence sender ──────────────────────────────────

async def send_auto_dm_sequence(
    author: discord.User | discord.Member,
    message: str,
    delay_min: float,
    delay_max: float,
    part_delay_min: float,
    part_delay_max: float,
) -> bool:
    """
    Split `message` on newlines, send each line as its own DM.

    Captcha behaviour:
      • Captcha hit → immediately pause gate + Telegram alert + await /resume.
      • After /resume → one final retry on the same part.
      • If that also fails → abort sequence.
      • All progress persisted to dm_queue so a process crash doesn't lose position.

    Returns True only if every part was sent successfully.
    """
    parts = [line.strip() for line in message.split("\n") if line.strip()]
    if not parts:
        logger.warning(
            "send_auto_dm_sequence called with empty message for %s", author.id
        )
        return False

    total = len(parts)
    username = str(author)

    # ── 1. persist queue row before touching Discord ───────────────────────
    queue_id = storage.queue_create(author.id, username, parts)
    logger.info(
        "dm_queue row %d created for %s (%d part(s))", queue_id, username, total
    )

    # ── 2. initial delay ───────────────────────────────────────────────────
    delay = random.uniform(delay_min, delay_max)
    if delay > 0:
        logger.info(
            "Waiting %.1fs before DM sequence to %s (%s)", delay, author, author.id
        )
        await asyncio.sleep(delay)

    # ── 3. iterate over parts ─────────────────────────────────────────────
    index = 0
    while index < total:
        part = parts[index]
        human_index = index + 1      # 1-based for logging

        # ── 3a. honour any active pause before attempting a send ───────────
        if gate.is_paused:
            logger.info(
                "Gate is paused before part %d/%d for %s — waiting for /resume",
                human_index, total, username,
            )
            await gate.wait()
            logger.info("Gate opened — continuing DM to %s", username)

        # ── 3b. attempt send ───────────────────────────────────────────────
        sent = await _send_part(
            author=author,
            part=part,
            part_human_index=human_index,
            total=total,
            queue_id=queue_id,
            index=index,
            username=username,
        )

        if sent is False:
            storage.queue_update_progress(queue_id, index, "paused")
            return False

        # ── 3c. part sent — advance queue ──────────────────────────────────
        index += 1
        storage.queue_update_progress(queue_id, index, "pending")

        # ── 3d. inter-part delay (skip after the last part) ───────────────
        if index < total:
            gap = random.uniform(part_delay_min, part_delay_max)
            if gap > 0:
                logger.info(
                    "Waiting %.1fs before next part to %s (%s)", gap, author, author.id
                )
                await asyncio.sleep(gap)

    # ── 4. all parts done ─────────────────────────────────────────────────
    storage.queue_update_progress(queue_id, total, "done")
    logger.info(
        "DM sequence complete for %s (%s) — %d part(s) sent", author, author.id, total
    )
    return True


# ── internal: one part, immediate captcha escalation ─────────────────────

async def _send_part(
    author: discord.User | discord.Member,
    part: str,
    part_human_index: int,
    total: int,
    queue_id: int,
    index: int,
    username: str,
) -> bool:
    """
    Try to send one part. Returns:
        True  — sent successfully
        False — fatal error, caller should abort

    Captcha path (no backoff):
        attempt 0 → captcha → pause gate + Telegram alert + await /resume
        attempt 1 (post-resume) → success → True
                                → captcha again → False (give up)
        Non-captcha / Forbidden → False immediately
    """
    for attempt in range(2):    # 0 = first try, 1 = post-resume retry
        try:
            await author.send(part)
            logger.info(
                "Sent DM part %d/%d to %s (%s)",
                part_human_index, total, author, author.id,
            )
            return True

        except discord.Forbidden:
            logger.warning(
                "DM part %d/%d forbidden for %s (%s) — DMs closed or blocked",
                part_human_index, total, author, author.id,
            )
            return False

        except discord.HTTPException as exc:
            error_str = str(exc)

            if not _is_captcha_error(exc):
                logger.error(
                    "DM part %d/%d failed for %s (%s): %s",
                    part_human_index, total, author, author.id, exc,
                )
                return False

            # ── captcha hit ────────────────────────────────────────────────
            logger.warning(
                "Captcha on part %d/%d for %s (%s): %s",
                part_human_index, total, author, author.id, exc,
            )

            if attempt == 0:
                # First hit — pause immediately, alert Telegram, wait for /resume
                ctx = PauseContext(
                    user_id=author.id,
                    username=username,
                    part_index=index,
                    total_parts=total,
                    last_error=error_str,
                )
                gate.pause(ctx)
                storage.queue_update_progress(queue_id, index, "paused")

                await telegram_bot.notify_captcha(
                    username=username,
                    user_id=author.id,
                    part=part_human_index,
                    total=total,
                    error=error_str,
                )

                logger.info("Awaiting /resume from Telegram for %s", username)
                await gate.wait()
                logger.info(
                    "Resumed — retrying part %d/%d for %s",
                    part_human_index, total, username,
                )
                storage.queue_update_progress(queue_id, index, "pending")
                continue    # attempt 1: retry after manual resume

            # attempt == 1: captcha still firing after manual resume → give up
            logger.error(
                "Captcha still firing after manual resume for %s (%s) — aborting part %d/%d",
                author, author.id, part_human_index, total,
            )
            return False

    return False    # unreachable but satisfies type checker