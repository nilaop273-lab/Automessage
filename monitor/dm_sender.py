from __future__ import annotations

# *dm_sender.py — DM delivery engine with queue-based captcha pause/resume*

import asyncio
import itertools
import logging
import random

import discord

from monitor import storage
from monitor.captcha_solver import (
    parse_captcha_from_exception,
    send_dm_with_captcha,
    solve_with_nonecap,
)
from tg import telegram_bot
from tg.state import captcha_queue, Waiter

logger = logging.getLogger(__name__)

_rotation_cycle:   itertools.cycle | None = None
_rotation_pool_id: int | None = None

# set via configure() from main once settings are loaded
_discord_token: str | None = None
_nonecap_key: str | None = None
_nonecap_max_attempts: int = 2


def configure(
    discord_token: str,
    nonecap_key: str | None = None,
    nonecap_max_attempts: int = 2,
) -> None:
    """Called once at startup so captcha auto-solve can use the user token + API key."""
    global _discord_token, _nonecap_key, _nonecap_max_attempts
    _discord_token = discord_token
    _nonecap_key = nonecap_key or None
    _nonecap_max_attempts = max(1, min(int(nonecap_max_attempts), 5))
    if _nonecap_key:
        logger.info(
            "[DM] NoneCap auto-captcha enabled (max_attempts=%d)",
            _nonecap_max_attempts,
        )
    else:
        logger.info("[DM] NoneCap auto-captcha disabled — telegram /resume only")


# ── public helpers ────────────────────────────────────────────────────────

def is_on_cooldown(user_id: int, cooldown_seconds: int) -> bool:
    return storage.is_user_on_cooldown(user_id, cooldown_seconds)


def record_dm(user_id: int) -> None:
    storage.record_user_dm(user_id)


def pick_message(messages: list[str], mode: str = "random") -> str:
    global _rotation_cycle, _rotation_pool_id

    if not messages:
        raise ValueError("Message pool is empty")
    if len(messages) == 1:
        return messages[0]
    if mode == "random":
        return random.choice(messages)

    pool_id = id(messages)
    if _rotation_cycle is None or _rotation_pool_id != pool_id:
        _rotation_cycle   = itertools.cycle(messages)
        _rotation_pool_id = pool_id
    return next(_rotation_cycle)


# ── captcha fingerprinting ────────────────────────────────────────────────

def _is_captcha_error(exc: discord.HTTPException) -> bool:
    err_text = str(exc).lower()
    return (
        "captcha" in err_text
        or getattr(exc, "code", None) == -1
        or (exc.status == 400 and "captcha" in err_text)
    )


# ── single DM (unchanged public API) ─────────────────────────────────────

async def send_auto_dm(
    author: discord.User | discord.Member,
    message: str,
    delay_min: float,
    delay_max: float,
) -> bool:
    delay = random.uniform(delay_min, delay_max)
    if delay > 0:
        logger.info("[DM] Waiting %.1fs before single DM → %s (id: %s)", delay, author, author.id)
        await asyncio.sleep(delay)

    try:
        await author.send(message)
        logger.info("[DM] ✓ Single DM sent → %s (id: %s)", author, author.id)
        return True
    except discord.Forbidden:
        logger.warning("[DM] ✗ Forbidden → %s (id: %s) — DMs closed or blocked", author, author.id)
        return False
    except discord.HTTPException as exc:
        logger.error("[DM] ✗ HTTP error → %s (id: %s): %s", author, author.id, exc)
        return False


# ── sequence sender ───────────────────────────────────────────────────────

async def send_auto_dm_sequence(
    author: discord.User | discord.Member,
    message: str,
    delay_min: float,
    delay_max: float,
    part_delay_min: float,
    part_delay_max: float,
) -> bool:
    parts = [line.strip() for line in message.split("\n") if line.strip()]
    if not parts:
        logger.warning("[DM] Empty message for %s (id: %s) — skipping", author, author.id)
        return False

    total    = len(parts)
    username = str(author)

    # persist before touching Discord
    queue_id = storage.queue_create(author.id, username, parts)
    logger.info(
        "[DM] Queue row %d created → %s (id: %s) | %d part(s)",
        queue_id, username, author.id, total,
    )

    # initial delay
    delay = random.uniform(delay_min, delay_max)
    if delay > 0:
        logger.info("[DM] Initial delay %.1fs → %s (id: %s)", delay, author, author.id)
        await asyncio.sleep(delay)

    index = 0
    while index < total:
        part        = parts[index]
        human_index = index + 1

        sent = await _send_part(
            author          = author,
            part            = part,
            part_human_index= human_index,
            total           = total,
            queue_id        = queue_id,
            index           = index,
            username        = username,
        )

        if sent is False:
            storage.queue_update_progress(queue_id, index, "paused")
            logger.warning(
                "[DM] Sequence aborted at part %d/%d → %s (id: %s)",
                human_index, total, username, author.id,
            )
            return False

        index += 1
        storage.queue_update_progress(queue_id, index, "pending")

        if index < total:
            gap = random.uniform(part_delay_min, part_delay_max)
            if gap > 0:
                logger.info(
                    "[DM] Part delay %.1fs before part %d/%d → %s (id: %s)",
                    gap, index + 1, total, author, author.id,
                )
                await asyncio.sleep(gap)

    storage.queue_update_progress(queue_id, total, "done")
    logger.info(
        "[DM] ✓ All %d part(s) delivered → %s (id: %s)",
        total, author, author.id,
    )
    return True


# ── auto captcha via NoneCap ──────────────────────────────────────────────

async def _try_nonecap_solve(
    author: discord.User | discord.Member,
    part: str,
    exc: discord.HTTPException,
    part_human_index: int,
    total: int,
) -> bool:
    """
    Attempt up to _nonecap_max_attempts NoneCap solves + raw REST retries.
    Returns True if the part was delivered. False → caller should fall back
    to the Telegram /resume queue.
    """
    if not _nonecap_key or not _discord_token:
        return False

    challenge = parse_captcha_from_exception(exc)
    if challenge is None:
        logger.warning(
            "[DM] Captcha payload unparseable for %s — skipping auto-solve. raw=%s",
            author.id,
            str(exc)[:300],
        )
        return False

    for n in range(1, _nonecap_max_attempts + 1):
        logger.info(
            "[DM] NoneCap attempt %d/%d for part %d/%d → %s",
            n,
            _nonecap_max_attempts,
            part_human_index,
            total,
            author,
        )
        solution = await solve_with_nonecap(_nonecap_key, challenge)
        if solution is None:
            logger.warning("[DM] NoneCap attempt %d returned no token", n)
            continue

        ok, detail = await send_dm_with_captcha(
            _discord_token,
            author.id,
            part,
            solution,
        )
        if ok:
            logger.info(
                "[DM] ✓ Part %d/%d sent via NoneCap → %s (id: %s)",
                part_human_index,
                total,
                author,
                author.id,
            )
            return True

        logger.warning(
            "[DM] NoneCap token rejected/failed attempt %d: %s",
            n,
            detail[:400],
        )
        # if discord returned a fresh captcha payload, refresh challenge
        # (detail may contain json); best-effort re-parse
        maybe = parse_captcha_from_exception(Exception(detail))
        if maybe is not None:
            challenge = maybe

    logger.warning(
        "[DM] NoneCap exhausted %d attempt(s) for %s — falling back to Telegram",
        _nonecap_max_attempts,
        author,
    )
    return False


# ── one part with captcha escalation ─────────────────────────────────────

async def _send_part(
    author:           discord.User | discord.Member,
    part:             str,
    part_human_index: int,
    total:            int,
    queue_id:         int,
    index:            int,
    username:         str,
) -> bool:
    # attempt 0 = first try (+ optional NoneCap)
    # attempt 1 = after Telegram /resume
    for attempt in range(2):
        try:
            await author.send(part)
            logger.info(
                "[DM] ✓ Part %d/%d sent → %s (id: %s)%s",
                part_human_index, total, author, author.id,
                " [post-resume]" if attempt == 1 else "",
            )
            return True

        except discord.Forbidden:
            logger.warning(
                "[DM] ✗ Part %d/%d forbidden → %s (id: %s) — DMs closed or blocked",
                part_human_index, total, author, author.id,
            )
            return False

        except discord.HTTPException as exc:
            error_str = str(exc)

            if not _is_captcha_error(exc):
                logger.error(
                    "[DM] ✗ Part %d/%d HTTP error → %s (id: %s): %s",
                    part_human_index, total, author, author.id, exc,
                )
                return False

            # ── captcha ────────────────────────────────────────────────────
            logger.warning(
                "[DM] ⚠ Captcha on part %d/%d → %s (id: %s): %s",
                part_human_index, total, author, author.id, exc,
            )

            # Auto-solve first (only on the initial attempt — after /resume
            # the human already cleared the client-side captcha).
            if attempt == 0:
                auto_ok = await _try_nonecap_solve(
                    author, part, exc, part_human_index, total
                )
                if auto_ok:
                    return True

                # fall through to Telegram pause
                waiter = Waiter(
                    user_id    = author.id,
                    username   = username,
                    part_index = index,
                    total_parts= total,
                    last_error = error_str,
                )
                captcha_queue.add(waiter)
                storage.queue_update_progress(queue_id, index, "paused")

                logger.warning(
                    "[DM] Captcha queue depth: %d — awaiting /resume for %s (id: %s)",
                    captcha_queue.pending, username, author.id,
                )

                await telegram_bot.notify_captcha(
                    username   = username,
                    user_id    = author.id,
                    part       = part_human_index,
                    total      = total,
                    error      = error_str,
                    queue_size = captcha_queue.pending,
                )

                await waiter.wait()

                if waiter.skipped:
                    logger.info(
                        "[DM] Sequence skipped via /skip → %s (id: %s) — aborting part %d/%d",
                        username, author.id, part_human_index, total,
                    )
                    storage.queue_update_progress(queue_id, index, "paused")
                    return False

                logger.info(
                    "[DM] Resumed → retrying part %d/%d for %s (id: %s)",
                    part_human_index, total, username, author.id,
                )
                storage.queue_update_progress(queue_id, index, "pending")
                continue

            # attempt 1 still captcha — give up
            logger.error(
                "[DM] ✗ Captcha persists after resume → %s (id: %s) — aborting part %d/%d",
                author, author.id, part_human_index, total,
            )
            return False

    return False
