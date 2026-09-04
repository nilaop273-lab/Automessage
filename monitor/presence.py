# [context: discord-selfbot-monitor, os: linux, arch: x86_64]
import logging
from typing import Optional

import discord

log = logging.getLogger("presence")

# activity type map for telegram commands
ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
    "streaming": discord.ActivityType.streaming,
}


class PresenceManager:
    """
    Holds current presence state and applies it to the Discord client.
    Controlled via Telegram: /status playing <text> etc.
    """

    def __init__(self):
        self.enabled = True
        self.activity_type = "playing"
        self.activity_text = ""
        self.status = discord.Status.online  # online / idle / dnd / invisible
        self._client: Optional[discord.Client] = None

    def attach(self, client: discord.Client):
        self._client = client

    def describe(self) -> str:
        if not self.enabled or not self.activity_text:
            return f"presence: off (status={self.status})"
        return (
            f"presence: {self.activity_type} {self.activity_text!r} "
            f"(status={self.status})"
        )

    async def apply(self):
        if self._client is None or self._client.user is None:
            return
        try:
            if not self.enabled or not self.activity_text:
                await self._client.change_presence(
                    activity=None, status=self.status
                )
                log.info("[PRESENCE] cleared — %s", self.describe())
                return

            atype = ACTIVITY_TYPES.get(
                self.activity_type.lower(), discord.ActivityType.playing
            )
            if atype == discord.ActivityType.streaming:
                activity = discord.Streaming(
                    name=self.activity_text,
                    url="https://twitch.tv/discord",
                )
            else:
                activity = discord.Activity(type=atype, name=self.activity_text)
            await self._client.change_presence(
                activity=activity, status=self.status
            )
            log.info("[PRESENCE] applied — %s", self.describe())
        except Exception as exc:
            log.error("[PRESENCE] apply failed: %s: %s", type(exc).__name__, exc)

    async def set_activity(self, activity_type: str, text: str):
        activity_type = (activity_type or "playing").strip().lower()
        if activity_type not in ACTIVITY_TYPES:
            activity_type = "playing"
        self.activity_type = activity_type
        self.activity_text = (text or "").strip()
        self.enabled = bool(self.activity_text)
        await self.apply()

    async def set_status(self, status_name: str):
        mapping = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
            "offline": discord.Status.invisible,
        }
        self.status = mapping.get(
            (status_name or "online").strip().lower(), discord.Status.online
        )
        await self.apply()

    async def clear(self):
        self.enabled = False
        self.activity_text = ""
        await self.apply()
