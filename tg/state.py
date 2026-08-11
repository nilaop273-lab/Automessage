from __future__ import annotations

# *state.py — the single nerve between dm_sender and telegram_bot*
# dm_sender clears the gate when captcha hits. telegram_bot sets it on /resume.
# Both import the same `gate` singleton. No IPC, no pipes, no polling —
# same process, same event loop, one asyncio.Event doing all the work.

import asyncio
from dataclasses import dataclass


@dataclass
class PauseContext:
    """Snapshot of what got frozen so the Telegram message is informative."""
    user_id: int = 0
    username: str = ""
    part_index: int = 0       # 0-based index of the part that stalled
    total_parts: int = 0
    last_error: str = ""


class _PauseGate:
    """
    Thin wrapper around asyncio.Event.

    State machine:
        OPEN  (event.is_set()  == True)  — normal run, nothing paused
        CLOSED (event.is_set() == False) — captcha hit, dm_sender is blocked

    dm_sender  →  gate.pause(ctx)   to freeze
    telegram   →  gate.resume()     to unfreeze
    dm_sender  →  await gate.wait() to block until open
    """

    def __init__(self) -> None:
        # Lazy-init after the event loop is running.
        self._event: asyncio.Event | None = None
        self.context: PauseContext = PauseContext()

    def _ev(self) -> asyncio.Event:
        if self._event is None:
            self._event = asyncio.Event()
            self._event.set()          # starts OPEN
        return self._event

    @property
    def is_paused(self) -> bool:
        return not self._ev().is_set()

    def pause(self, ctx: PauseContext) -> None:
        """Freeze the gate and record what got stuck."""
        self.context = ctx
        self._ev().clear()

    def resume(self) -> None:
        """Open the gate — dm_sender wakes up on the next await."""
        self._ev().set()

    async def wait(self) -> None:
        """Block until the gate is open. No-op if already open."""
        await self._ev().wait()


# ── singleton ──────────────────────────────────────────────────────────────
gate = _PauseGate()