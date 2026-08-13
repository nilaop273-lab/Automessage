from __future__ import annotations

import re
import logging
from datetime import timezone

import discord

from config import Settings
from monitor import dm_sender, storage

logger = logging.getLogger(__name__)

REQUEST_PATTERN    = re.compile(r"request by:\s*<@!?(\d+)>", re.IGNORECASE)
_MARKDOWN_EMPHASIS = re.compile(r"\*{1,3}|_{1,3}|~~")


# ── text helpers ───────────────────────────────────────────────────────────

def strip_markdown_emphasis(text: str) -> str:
    return _MARKDOWN_EMPHASIS.sub("", text)


async def resolve_message_content(message: discord.Message) -> str:
    if message.content:
        return message.content
    try:
        async for fetched in message.channel.history(limit=1, around=message):
            if fetched.id == message.id:
                return fetched.content or ""
    except discord.HTTPException as exc:
        logger.warning(
            "[MSG %s] Could not fetch content from history: %s",
            message.id, exc,
        )
    return ""


def extract_embed_text(message: discord.Message) -> str:
    parts: list[str] = []
    for embed in message.embeds:
        for attr in (embed.title, embed.description):
            if attr:
                parts.append(str(attr))
        for field in embed.fields:
            if field.name:  parts.append(str(field.name))
            if field.value: parts.append(str(field.value))
        if embed.footer and embed.footer.text:
            parts.append(str(embed.footer.text))
        if embed.author and embed.author.name:
            parts.append(str(embed.author.name))
    return "\n".join(parts)


def passes_keyword_filter(content: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    lowered = content.lower()
    return any(kw in lowered for kw in keywords)


# ── resolve user ───────────────────────────────────────────────────────────

async def resolve_user(
    client: discord.Client,
    message: discord.Message,
    user_id: int,
) -> discord.User | discord.Member | None:
    for mention in message.mentions:
        if mention.id == user_id:
            return mention

    if message.guild:
        try:
            return await message.guild.fetch_member(user_id)
        except discord.HTTPException:
            pass

    try:
        logger.debug("[USER] Fetching user %s via API", user_id)
        return await client.fetch_user(user_id)
    except discord.NotFound:
        logger.warning("[USER] User %s not found", user_id)
    except discord.HTTPException as exc:
        logger.error("[USER] HTTP error fetching user %s: %s", user_id, exc)

    return None


# ── paid editor request handler ────────────────────────────────────────────

async def handle_paid_editor_request(
    message: discord.Message,
    settings: Settings,
    client: discord.Client,
) -> None:
    if settings.paid_request_channel_id is None:
        return
    if message.channel.id != settings.paid_request_channel_id:
        return
    if message.author.name != settings.paid_request_trigger_author:
        return

    # ── duplicate guard ────────────────────────────────────────────────────
    if storage.is_message_processed(message.id, kind="paid_request"):
        logger.debug("[PAID] Msg %s already processed — skipping", message.id)
        return
    storage.mark_message_processed(message.id, kind="paid_request")

    logger.info(
        "[PAID] Trigger from %s in channel %s",
        message.author, message.channel.id,
    )

    content    = await resolve_message_content(message)
    embed_text = extract_embed_text(message)
    search_text = strip_markdown_emphasis(f"{content}\n{embed_text}")

    match = REQUEST_PATTERN.search(search_text)
    if not match:
        logger.warning("[PAID] Msg %s — no 'request by: <@id>' pattern found", message.id)
        return

    user_id = int(match.group(1))
    logger.info("[PAID] Requester found → user_id: %s", user_id)

    if dm_sender.is_on_cooldown(user_id, settings.dm_cooldown_seconds):
        logger.info("[PAID] User %s on cooldown — DM skipped", user_id)
        return

    # ── duplicate DM guard ─────────────────────────────────────────────────
    if not storage.claim_dm_slot(user_id):
        logger.info("[PAID] User %s — duplicate blocked (already claimed this session)", user_id)
        return

    try:
        user = await resolve_user(client, message, user_id)
        if user is None:
            logger.warning("[PAID] Could not resolve user %s — aborting", user_id)
            return

        logger.info("  [PAID] → sending DM to %s (id: %s)", user.name, user.id)

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
            logger.info("  [PAID] ✓ DM complete → %s (id: %s)", user.name, user.id)
        else:
            logger.warning("  [PAID] ✗ DM incomplete → %s (id: %s)", user.name, user.id)

    except discord.HTTPException as exc:
        logger.error("[PAID] HTTP error for user %s: %s", user_id, exc)
    except Exception as exc:
        logger.error("[PAID] Unexpected error for user %s: %s", user_id, exc)


# ── main message handler ───────────────────────────────────────────────────

async def handle_incoming_message(
    message: discord.Message,
    settings: Settings,
    self_user_id: int,
    client: discord.Client,
) -> None:
    # paid request runs independently of the monitored-channel filter
    await handle_paid_editor_request(message, settings, client)

    if message.channel.id not in settings.monitored_channel_ids:
        return
    if settings.ignore_bot_messages and message.author.bot:
        return
    if message.author.id == self_user_id:
        return

    # ── duplicate guard ────────────────────────────────────────────────────
    if storage.is_message_processed(message.id, kind="auto_dm"):
        logger.debug("[DM] Msg %s already processed — skipping", message.id)
        return
    storage.mark_message_processed(message.id, kind="auto_dm")

    content = await resolve_message_content(message)

    if not passes_keyword_filter(content, settings.keyword_filter):
        logger.debug(
            "[DM] Msg %s from %s — keyword filter rejected",
            message.id, message.author,
        )
        return

    # ── log the incoming message ───────────────────────────────────────────
    ts      = message.created_at.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
    channel = getattr(message.channel, "name", str(message.channel.id))
    guild   = message.guild.name if message.guild else "DM"

    att_lines = ""
    for a in message.attachments:
        att_lines += f"\n  file      {a.filename}  →  {a.url}"

    logger.info(
        "\n"
        "  ┌─ NEW MESSAGE ───────────────────────────────\n"
        "  │  time      %s  •  #%s @ %s\n"
        "  │  author    %s  (id: %s)\n"
        "  │  content   %s%s\n"
        "  └────────────────────────────────────────────",
        ts, channel, guild,
        message.author, message.author.id,
        content or "<empty>", att_lines,
    )

    if not settings.auto_dm_enabled:
        logger.debug("[DM] Auto-DM disabled — skipping")
        return

    author = message.author

    if dm_sender.is_on_cooldown(author.id, settings.dm_cooldown_seconds):
        logger.info("  skipping %s (id: %s) — on cooldown", author, author.id)
        return

    # ── duplicate DM guard ─────────────────────────────────────────────────
    # Atomically claim the slot — if two messages from different channels
    # arrive simultaneously for the same user, only one coroutine wins here.
    if not storage.claim_dm_slot(author.id):
        logger.info("  skipping %s (id: %s) — duplicate blocked", author, author.id)
        return

    dm_text = dm_sender.pick_message(settings.auto_dm_messages, settings.auto_dm_rotation)

    logger.info("  → sending DM to %s (id: %s)", author, author.id)

    sent = await dm_sender.send_auto_dm_sequence(
        author,
        dm_text,
        settings.dm_delay_min_seconds,
        settings.dm_delay_max_seconds,
        settings.dm_part_delay_min_seconds,
        settings.dm_part_delay_max_seconds,
    )

    if sent:
        dm_sender.record_dm(author.id)
        logger.info("  ✓ DM complete → %s (id: %s)", author, author.id)
    else:
        logger.warning("  ✗ DM incomplete → %s (id: %s)", author, author.id)