from __future__ import annotations

import logging
import sys

import discord

from config import load_settings
from monitor.client import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> None:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    client = create_client(settings)

    try:
        client.run(settings.token)
    except discord.LoginFailure as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
