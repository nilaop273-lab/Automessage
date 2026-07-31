import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _parse_channel_ids(raw: str) -> set[int]:
    if not raw.strip():
        return set()
    return {int(channel_id.strip()) for channel_id in raw.split(",") if channel_id.strip()}


def _parse_keywords(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [keyword.strip().lower() for keyword in raw.split(",") if keyword.strip()]


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    token: str
    monitored_channel_ids: set[int]
    ignore_bot_messages: bool
    keyword_filter: list[str]
    auto_dm_enabled: bool
    auto_dm_message: str
    dm_cooldown_seconds: int
    dm_delay_min_seconds: float
    dm_delay_max_seconds: float


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "your_user_token_here":
        raise ValueError(
            "Set DISCORD_TOKEN in .env (copy .env.example and add your user token)."
        )

    channel_ids = _parse_channel_ids(os.getenv("MONITORED_CHANNEL_IDS", ""))
    if not channel_ids:
        raise ValueError(
            "Set MONITORED_CHANNEL_IDS in .env with at least one channel ID."
        )

    ignore_bots = _parse_bool(os.getenv("IGNORE_BOT_MESSAGES"), default=True)
    auto_dm_enabled = _parse_bool(os.getenv("AUTO_DM_ENABLED"), default=True)
    auto_dm_message = os.getenv("AUTO_DM_MESSAGE", "").strip()
    dm_cooldown_seconds = int(os.getenv("DM_COOLDOWN_SECONDS", "86400"))
    dm_delay_min_seconds = float(os.getenv("DM_DELAY_MIN_SECONDS", "5"))
    dm_delay_max_seconds = float(os.getenv("DM_DELAY_MAX_SECONDS", "15"))

    if auto_dm_enabled and not auto_dm_message:
        raise ValueError(
            "Set AUTO_DM_MESSAGE in .env when AUTO_DM_ENABLED is true."
        )

    if dm_cooldown_seconds < 0:
        raise ValueError("DM_COOLDOWN_SECONDS must be zero or greater.")

    if dm_delay_min_seconds < 0 or dm_delay_max_seconds < 0:
        raise ValueError("DM delay seconds must be zero or greater.")

    if dm_delay_min_seconds > dm_delay_max_seconds:
        raise ValueError("DM_DELAY_MIN_SECONDS must be less than or equal to DM_DELAY_MAX_SECONDS.")

    return Settings(
        token=token,
        monitored_channel_ids=channel_ids,
        ignore_bot_messages=ignore_bots,
        keyword_filter=_parse_keywords(os.getenv("KEYWORD_FILTER")),
        auto_dm_enabled=auto_dm_enabled,
        auto_dm_message=auto_dm_message,
        dm_cooldown_seconds=dm_cooldown_seconds,
        dm_delay_min_seconds=dm_delay_min_seconds,
        dm_delay_max_seconds=dm_delay_max_seconds,
    )
