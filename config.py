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


def _parse_message_pool(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    messages = [m.strip() for m in raw.split("|||") if m.strip()]
    # Convert literal \n from .env into real newlines
    return [m.replace("\\n", "\n") for m in messages]


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
    # --- paid editor request forwarding ---
    paid_request_channel_id: int | None
    paid_request_trigger_author: str
    paid_request_dm_messages: list[str]
    paid_request_dm_rotation: str
    paid_request_part_delay_min_seconds: float
    paid_request_part_delay_max_seconds: float


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

    raw_paid_channel = os.getenv("PAID_REQUEST_CHANNEL_ID", "").strip()
    paid_request_channel_id = int(raw_paid_channel) if raw_paid_channel else None

    paid_request_trigger_author = os.getenv("PAID_REQUEST_TRIGGER_AUTHOR", "nick.editz_").strip()

    paid_request_dm_messages = _parse_message_pool(os.getenv("PAID_REQUEST_DM_MESSAGES"))
    if not paid_request_dm_messages:
        # Fall back to a single legacy message, then to the generic auto-DM message
        single = os.getenv("PAID_REQUEST_DM_MESSAGE", "").strip()
        if single:
            paid_request_dm_messages = [single.replace("\\n", "\n")]
        elif auto_dm_message:
            paid_request_dm_messages = [auto_dm_message]

    paid_request_dm_rotation = os.getenv("PAID_REQUEST_DM_ROTATION", "random").strip().lower()
    if paid_request_dm_rotation not in {"random", "sequential"}:
        raise ValueError("PAID_REQUEST_DM_ROTATION must be 'random' or 'sequential'.")

    paid_request_part_delay_min_seconds = float(os.getenv("PAID_REQUEST_PART_DELAY_MIN_SECONDS", "4"))
    paid_request_part_delay_max_seconds = float(os.getenv("PAID_REQUEST_PART_DELAY_MAX_SECONDS", "10"))

    if paid_request_part_delay_min_seconds < 0 or paid_request_part_delay_max_seconds < 0:
        raise ValueError("PAID_REQUEST_PART_DELAY seconds must be zero or greater.")

    if paid_request_part_delay_min_seconds > paid_request_part_delay_max_seconds:
        raise ValueError(
            "PAID_REQUEST_PART_DELAY_MIN_SECONDS must be less than or equal to "
            "PAID_REQUEST_PART_DELAY_MAX_SECONDS."
        )

    if paid_request_channel_id and not paid_request_dm_messages:
        raise ValueError(
            "Set PAID_REQUEST_DM_MESSAGES (or PAID_REQUEST_DM_MESSAGE / AUTO_DM_MESSAGE) "
            "when PAID_REQUEST_CHANNEL_ID is set."
        )

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
        paid_request_channel_id=paid_request_channel_id,
        paid_request_trigger_author=paid_request_trigger_author,
        paid_request_dm_messages=paid_request_dm_messages,
        paid_request_dm_rotation=paid_request_dm_rotation,
        paid_request_part_delay_min_seconds=paid_request_part_delay_min_seconds,
        paid_request_part_delay_max_seconds=paid_request_part_delay_max_seconds,
    )