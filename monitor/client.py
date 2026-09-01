# [context: discord-selfbot-monitor, os: linux, arch: x86_64]
import logging

import discord

from monitor.handlers import handle_message

log = logging.getLogger("monitor")


class MonitorClient(discord.Client):
    def __init__(self, settings, storage, dm_sender=None):
        # No Intents — selfbots do not need them.
        # The previous working version used super().__init__() bare.
        super().__init__()
        self.cfg = settings
        self._storage = storage
        self._dm_sender = dm_sender

    async def on_ready(self):
        user = self.user
        name = user.name if user else "unknown"
        uid = user.id if user else "?"

        banner = (
            "┌─ SELF BOT STARTED ─\n"
            f"│ account={name} ({uid})\n"
            f"│ channel={self.cfg.paid_request_channel_id}\n"
            f"│ trigger_author={self.cfg.paid_request_trigger_author}\n"
            f"│ cooldown={self.cfg.dm_cooldown_seconds}s\n"
            f"│ initial_delay={self.cfg.dm_delay_min_seconds}-"
            f"{self.cfg.dm_delay_max_seconds}s\n"
            f"│ part_delay={self.cfg.part_delay_min_seconds}-"
            f"{self.cfg.part_delay_max_seconds}s\n"
            f"│ pool_size={len(self.cfg.dm_messages)}\n"
            "└─ READY"
        )
        log.info("[PAID] %s", banner)

    async def on_message(self, message):
        if self.user is None or message.author.id == self.user.id:
            return

        if message.channel.id != self.cfg.paid_request_channel_id:
            return

        content = getattr(message, "content", "") or ""
        embeds = getattr(message, "embeds", []) or []

        log.info(
            "[PAID] saw message in channel "
            "message_id=%s author_name=%r display_name=%r "
            "global_name=%r bot=%s content=%r embeds=%d",
            message.id,
            getattr(message.author, "name", None),
            getattr(message.author, "display_name", None),
            getattr(message.author, "global_name", None),
            getattr(message.author, "bot", False),
            content,
            len(embeds),
        )

        combined_content = content

        # If content and embeds are empty, try to get the referenced message
        if not content and not embeds:
            referenced = None
            ref = getattr(message, "reference", None)
            if ref is not None:
                ref_id = getattr(ref, "message_id", None)
                if ref_id:
                    try:
                        referenced = await message.channel.fetch_message(ref_id)
                    except Exception as exc:
                        log.error(
                            "[PAID] failed to fetch referenced message %s: %s: %s",
                            ref_id, type(exc).__name__, exc,
                        )
            if referenced is not None:
                ref_content = getattr(referenced, "content", "") or ""
                ref_embeds = getattr(referenced, "embeds", []) or []
                log.info(
                    "[PAID] referenced message found ref_id=%s content=%r embeds=%d",
                    referenced.id, ref_content, len(ref_embeds),
                )
                combined_content = ref_content
                if not combined_content:
                    buf = []
                    for e in ref_embeds:
                        t = getattr(e, "title", None)
                        d = getattr(e, "description", None)
                        if t:
                            buf.append(t)
                        if d:
                            buf.append(d)
                        for f in getattr(e, "fields", []) or []:
                            buf.append(f"{getattr(f, 'name', '')} {getattr(f, 'value', '')}")
                    combined_content = "\n".join(buf)

            if not combined_content:
                log.warning(
                    "[PAID] no content, no referenced text. Message has no usable payload."
                )

        if self._dm_sender is None:
            log.error("dm_sender not set — ignoring message")
            return

        await handle_message(
            message, self.cfg, self._storage, self._dm_sender, self,
            content_override=combined_content,
        )
