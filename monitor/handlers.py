from __future__ import annotations

import re
import logging
from datetime import timezone

import discord

from config import Settings
from monitor import dm_sender, storage

logger = logging.getLogger(__name__)

REQUEST_PATTERN = re.compile(r"request by:\s*<@!?(\d+)>", re.IGNORECASE)

# Discord markdown emphasis markers that can land right next to the target
# text (e.g. "**paid editor request by:** <@id>") and break whitespace-only
# gaps in regexes like REQUEST_PATTERN. Stripped before matching.
_MARKDOWN_EMPHASIS = re.compile(r"\*{1,3}|_{1,3}|~~")


def strip_markdown_emphasis(text: str) -> str:
    """Remove bold/italic/strikethrough markers so regex matching isn't
    thrown off by formatting placed around the target text."""
    return _MARKDOWN_EMPHASIS.sub("", text)


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


def extract_embed_text(message: discord.Message) -> str:
    """Pull all readable text out of a message's embeds (title, description, fields, footer)."""
    parts: list[str] = []
    for embed in message.embeds:
        if embed.title:
            parts.append(str(embed.title))
        if embed.description:
            parts.append(str(embed.description))
        for field in embed.fields:
            if field.name:
                parts.append(str(field.name))
            if field.value:
                parts.append(str(field.value))
        if embed.footer and embed.footer.text:
            parts.append(str(embed.footer.text))
        if embed.author and embed.author.name:
            parts.append(str(embed.author.name))
    return "\n".join(parts)


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


async def resolve_user(
    client: discord.Client,
    message: discord.Message,
    user_id: int,
) -> discord.User | discord.Member | None:
    """Resolve a user by ID: mentions -> guild member -> global fetch."""
    for mention in message.mentions:
        if mention.id == user_id:
            return mention

    guild = message.guild
    if guild:
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException:
            pass

    try:
        logger.info(f"Attempting to fetch user with ID: {user_id}")
        return await client.fetch_user(user_id)
    except discord.NotFound:
        logger.error(f"User not found with ID: {user_id}")
    except discord.HTTPException as e:
        logger.error(f"HTTP error occurred while fetching user ID {user_id}: {e}")

    return None


async def handle_paid_editor_request(
    message: discord.Message,
    settings: Settings,
    client: discord.Client,
) -> None:
    """Watches a specific channel/author for 'request by: <@id>' posts and
    DMs the requester, one line at a time as separate messages."""
    if settings.paid_request_channel_id is None:
        return
    if message.channel.id != settings.paid_request_channel_id:
        return
    if message.author.name != settings.paid_request_trigger_author:
        return

    logger.info(f"Received message from {message.author} in target channel {message.channel.id}")

    if storage.is_message_processed(message.id, kind="paid_request"):
        logger.info(f"Message {message.id} already processed, skipping")
        return
    storage.mark_message_processed(message.id, kind="paid_request")

    content = await resolve_message_content(message)
    logger.info(f"Message content: {content}")

    # Build embed text and the combined search text BEFORE the regex check,
    # so a "request by:" line that only lives inside an embed isn't missed.
    embed_text = extract_embed_text(message)
    combined_text = f"{content}\n{embed_text}"

    # Strip markdown emphasis (bold/italic/strikethrough) so formatting like
    # "**paid editor request by:** <@id>" doesn't break the whitespace-only
    # gap the regex expects between "by:" and the mention.
    search_text = strip_markdown_emphasis(combined_text)

    match = REQUEST_PATTERN.search(search_text)
    if not match:
        logger.info("Regex pattern did not match message content or embeds")
        return

    user_id = int(match.group(1))
    logger.info(f"Found requester with ID: {user_id}")

    if dm_sender.is_on_cooldown(user_id, settings.dm_cooldown_seconds):
        logger.info(f"DM rate limit exceeded for user {user_id}")
        return

    try:
        user = await resolve_user(client, message, user_id)
        if user is None:
            return

        logger.info(f"Successfully fetched user: {user.name} (ID: {user.id})")

        dm_text = dm_sender.pick_message(
            settings.paid_request_dm_messages,
            settings.paid_request_dm_rotation,
        )

        sent = await dm_sender.send_auto_dm_sequence(
            user,
            dm_text,
            settings.dm_delay_min_seconds,
            settings.dm_delay_max_seconds,
            settings.paid_request_part_delay_min_seconds,
            settings.paid_request_part_delay_max_seconds,
        )
        if sent:
            dm_sender.record_dm(user_id)
            logger.info(f"Successfully sent DM sequence to {user.name} (ID: {user.id})")
        else:
            logger.info(f"DM sequence not fully sent to {user.name} (ID: {user.id})")

    except discord.HTTPException as e:
        logger.error(f"HTTP error occurred while processing user ID {user_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error occurred while processing user ID {user_id}: {e}")


async def handle_incoming_message(
    message: discord.Message,
    settings: Settings,
    self_user_id: int,
    client: discord.Client,
) -> None:
    # Paid-editor-request flow runs independently of the monitored-channel filter below
    await handle_paid_editor_request(message, settings, client)

    if message.channel.id not in settings.monitored_channel_ids:
        return

    if settings.ignore_bot_messages and message.author.bot:
        return

    if message.author.id == self_user_id:
        return

    if storage.is_message_processed(message.id, kind="auto_dm"):
        logger.info(f"Message {message.id} already processed for auto-DM, skipping")
        return
    storage.mark_message_processed(message.id, kind="auto_dm")

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

    dm_text = dm_sender.pick_message(settings.auto_dm_messages, settings.auto_dm_rotation)

    if await dm_sender.send_auto_dm_sequence(
        author,
        dm_text,
        settings.dm_delay_min_seconds,
        settings.dm_delay_max_seconds,
        settings.dm_part_delay_min_seconds,
        settings.dm_part_delay_max_seconds,
    ):
        dm_sender.record_dm(author.id)
        logger.info("DM sequence sent to %s (%s)", author, author.id)
    else:
        logger.info("DM sequence not fully sent to %s (%s)", author, author.id)