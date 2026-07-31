from __future__ import annotations

import logging
from datetime import timezone

import discord

from config import Settings
from monitor import dm_sender

logger = logging.getLogger(__name__)


async def resolve_message_content(message: discord.Message) -> str:
    """Self-bot events sometimes omit content; history fetch is the workaround."""
    if message.content:
        return message.content

    try:
        async for fetched in message.channel.history(limit=1, around=message):
            if fetched.id == message.id:
                return fetched.content or ""
    except discord.HTTPException as exc:
        logger.warning("Could not fetch message %s from history: %s", message.id, exc)

    return ""


def passes_keyword_filter(content: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    lowered = content.lower()
    return any(keyword in lowered for keyword in keywords)


def format_message_log(message: discord.Message, content: str) -> str:
    timestamp = message.created_at.astimezone(timezone.utc).isoformat()
    author = f"{message.author} ({message.author.id})"
    channel = getattr(message.channel, "name", "unknown")
    guild = message.guild.name if message.guild else "DM"
    attachment_note = f" [{len(message.attachments)} attachment(s)]" if message.attachments else ""
    return (
        f"[{timestamp}] #{channel} @ {guild} | {author}{attachment_note}\n"
        f"{content or '<empty content>'}"
    )


async def handle_incoming_message(
    message: discord.Message,
    settings: Settings,
    self_user_id: int,
) -> None:
    if message.channel.id not in settings.monitored_channel_ids:
        return

    if settings.ignore_bot_messages and message.author.bot:
        return

    if message.author.id == self_user_id:
        return

    content = await resolve_message_content(message)

    if not passes_keyword_filter(content, settings.keyword_filter):
        return

    logger.info(format_message_log(message, content))

    for attachment in message.attachments:
        logger.info("  attachment: %s (%s)", attachment.filename, attachment.url)

    if not settings.auto_dm_enabled:
        return

    author = message.author
    if dm_sender.is_on_cooldown(author.id, settings.dm_cooldown_seconds):
        logger.info("DM skipped for %s (%s) — cooldown active", author, author.id)
        return

    if await dm_sender.send_auto_dm(
        author,
        settings.auto_dm_message,
        settings.dm_delay_min_seconds,
        settings.dm_delay_max_seconds,
    ):
        dm_sender.record_dm(author.id)
        logger.info("DM sent to %s (%s)", author, author.id)
