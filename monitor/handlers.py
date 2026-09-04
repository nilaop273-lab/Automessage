# [context: discord-selfbot-monitor, os: linux, arch: x86_64]
import logging
import re
import time

log = logging.getLogger("handlers")

# Normal Discord mention: request by: <@799...> or <@!799...>
REQUEST_PATTERN = re.compile(
    r"request by:\s*@?<@!?(\d+)>",
    re.IGNORECASE,
)
# Broken double form that some bots emit: request by: @<@799...>
REQUEST_PATTERN_DOUBLE = re.compile(
    r"request by:\s*@?<@!?(\d+)>",
    re.IGNORECASE,
)
# Plain username form: request by: @soup.xd_ or request by: soup.xd_
REQUEST_PATTERN_NAME = re.compile(
    r"request by:\s*@?([A-Za-z0-9._]+)",
    re.IGNORECASE,
)
# Extra: any bare snowflake after "request by"
REQUEST_PATTERN_ID_LOOSE = re.compile(
    r"request by:\s*@?<?@?!?(\d{17,20})>?",
    re.IGNORECASE,
)


def _strip_markdown(text):
    # keep underscores (usernames) — only strip real markdown wrappers
    for marker in ("***", "__", "~~", "**"):
        text = text.replace(marker, "")
    text = re.sub(r"(?<!\w)\*(?!\w)", "", text)
    return text


def _author_matches(message, trigger_author):
    author = message.author
    for attr in ("name", "display_name", "global_name"):
        value = getattr(author, attr, None)
        if value is not None and value == trigger_author:
            return True
    return False


def _combined_content(message, content_override=None):
    if content_override is not None:
        parts = [content_override]
    else:
        parts = [message.content or ""]
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
    return "\n".join(parts)


def _extract_target_user_id(message, content_override=None):
    combined = _combined_content(message, content_override)
    clean = _strip_markdown(combined)

    # 1) proper / double mention with snowflake  → int id (best)
    for pat in (REQUEST_PATTERN, REQUEST_PATTERN_DOUBLE, REQUEST_PATTERN_ID_LOOSE):
        match = pat.search(clean)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                pass

    # 2) plain username
    match = REQUEST_PATTERN_NAME.search(clean)
    if match:
        name = match.group(1)
        # skip if it looks like a pure snowflake we already failed on
        if name.isdigit() and len(name) >= 17:
            try:
                return int(name)
            except ValueError:
                pass
        return name

    return None


def _name_variants(name: str):
    name = (name or "").strip()
    if not name:
        return []
    variants = [name]
    stripped = name.rstrip("_")
    if stripped and stripped not in variants:
        variants.append(stripped)
    if name.startswith("@"):
        variants.append(name[1:])
    return variants


def _find_user_by_name(client, name):
    variants = _name_variants(name)
    if not variants:
        return None
    for guild in client.guilds:
        for member in guild.members:
            for attr in ("name", "display_name", "global_name"):
                value = getattr(member, attr, None)
                if value is None:
                    continue
                if value in variants or value.rstrip("_") in variants:
                    return member
    lower_variants = {v.lower() for v in variants}
    for guild in client.guilds:
        for member in guild.members:
            for attr in ("name", "display_name", "global_name"):
                value = getattr(member, attr, None)
                if value is None:
                    continue
                if (
                    value.lower() in lower_variants
                    or value.lower().rstrip("_") in lower_variants
                ):
                    return member
    return None


async def handle_message(
    message,
    settings,
    storage,
    dm_sender,
    client,
    content_override=None,
):
    if message.channel.id != settings.paid_request_channel_id:
        return
    if not _author_matches(message, settings.paid_request_trigger_author):
        log.info(
            "[PAID] author mismatch — got name=%r display=%r global=%r "
            "want=%r message_id=%s",
            getattr(message.author, "name", None),
            getattr(message.author, "display_name", None),
            getattr(message.author, "global_name", None),
            settings.paid_request_trigger_author,
            message.id,
        )
        return

    target = _extract_target_user_id(message, content_override)
    if target is None:
        log.info(
            "[PAID] author matched but request pattern missing "
            "message_id=%s raw_content=%r embeds=%d override=%r",
            message.id,
            message.content,
            len(message.embeds),
            content_override,
        )
        return

    if isinstance(target, int):
        user = client.get_user(target)
        if user is None:
            try:
                user = await client.fetch_user(target)
            except Exception as exc:
                log.error(
                    "[PAID] failed to resolve target user_id=%s exc=%s",
                    target,
                    exc,
                )
                return
    else:
        user = _find_user_by_name(client, target)
        if user is None:
            log.info(
                "[PAID] could not resolve target name=%r (variants=%s) via guilds — "
                "user is probably not sharing a server with the selfbot account",
                target,
                _name_variants(target),
            )
            return

    if not storage.mark_processed(message.id):
        log.debug("[PAID] duplicate message_id=%s", message.id)
        return

    log.info(
        "[PAID] ┌─ request detected\n"
        "[PAID] │ timestamp=%s\n"
        "[PAID] │ trigger_author=%s\n"
        "[PAID] │ target_user=%s (%s)\n"
        "[PAID] │ channel_id=%s message_id=%s",
        time.strftime("%Y-%m-%d %H:%M:%S"),
        message.author.name,
        user.name,
        user.id,
        message.channel.id,
        message.id,
    )

    remaining = storage.get_cooldown_remaining(
        user.id, settings.dm_cooldown_seconds
    )
    if remaining > 0:
        log.info(
            "[PAID] └─ skipped target_user=%s reason=cooldown "
            "remaining=%.0fs (%.1fh)",
            user.name,
            remaining,
            remaining / 3600.0,
        )
        return

    session_key = f"user-{user.id}"
    if not storage.claim_dm_slot(user.id, session_key):
        log.info(
            "[PAID] └─ skipped target_user=%s reason=duplicate_slot", user.name
        )
        return

    # pass full text so DM sender can detect Bloop / MEGALODON requirements
    trigger_text = content_override if content_override is not None else (
        message.content or ""
    )
    ok = await dm_sender.send_dm_sequence(
        user, message.author.name, session_key, trigger_text=trigger_text
    )
    if not ok:
        log.info("[PAID] └─ DM not delivered target_user=%s", user.name)
