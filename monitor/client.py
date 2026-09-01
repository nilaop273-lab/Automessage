from __future__ import annotations

import logging

import discord

from config import Settings
from monitor.handlers import handle_incoming_message

logger = logging.getLogger(__name__)


def create_client(settings: Settings) -> discord.Client:
    class MonitorClient(discord.Client):
        async def on_ready(self) -> None:
            user = self.user
            name = user.name if user else "unknown"
            uid  = user.id   if user else "?"

            ch_ids = sorted(settings.monitored_channel_ids)

            # auto-dm line
            if settings.auto_dm_enabled:
                dm_line = (
                    f"  auto-dm    ON  •  {len(settings.auto_dm_messages)} variant(s)"
                    f"  •  {settings.auto_dm_rotation}"
                    f"  •  cooldown {settings.dm_cooldown_seconds}s"
                    f"  •  delay {settings.dm_delay_min_seconds:.0f}–{settings.dm_delay_max_seconds:.0f}s"
                )
            else:
                dm_line = "  auto-dm    OFF"

            # paid request line
            if settings.paid_request_channel_id:
                paid_line = (
                    f"  paid req   channel {settings.paid_request_channel_id}"
                    f"  •  author: {settings.paid_request_trigger_author}"
                )
            else:
                paid_line = "  paid req   disabled"

            # keyword line
            kw_line = (
                f"  keywords   {', '.join(settings.keyword_filter)}"
                if settings.keyword_filter
                else "  keywords   none (all messages pass)"
            )

            # channel list
            ch_lines = "\n".join(f"               • {ch}" for ch in ch_ids)

            banner = (
                "\n"
                "  ╔══════════════════════════════════════════════╗\n"
                "  ║              BOT ONLINE                      ║\n"
                "  ╚══════════════════════════════════════════════╝\n"
                f"  account    {name}  (id: {uid})\n"
                f"  channels   {len(ch_ids)} monitored\n"
                f"{ch_lines}\n"
                f"{dm_line}\n"
                f"{paid_line}\n"
                f"{kw_line}\n"
                "  ══════════════════════════════════════════════"
            )

            logger.info(banner)

        async def on_message(self, message: discord.Message) -> None:
            if self.user is None:
                return
            await handle_incoming_message(message, settings, self.user.id, self)

    return MonitorClient()
