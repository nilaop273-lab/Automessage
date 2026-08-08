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
            logger.info("Logged in as %s (%s)", name, user.id if user else "?")
            logger.info(
                "Monitoring %d channel(s): %s",
                len(settings.monitored_channel_ids),
                ", ".join(str(channel_id) for channel_id in sorted(settings.monitored_channel_ids)),
            )
            if settings.auto_dm_enabled:
                logger.info(
                    "Auto-DM enabled (cooldown: %ds): %s",
                    settings.dm_cooldown_seconds,
                    settings.auto_dm_message,
                )
            if settings.paid_request_channel_id:
                logger.info(
                    "Paid-editor-request watcher enabled on channel %s for author '%s'",
                    settings.paid_request_channel_id,
                    settings.paid_request_trigger_author,
                )

        async def on_message(self, message: discord.Message) -> None:
            if self.user is None:
                return
            await handle_incoming_message(message, settings, self.user.id, self)

    return MonitorClient()