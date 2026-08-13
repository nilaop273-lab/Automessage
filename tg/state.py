from __future__ import annotations

# *state.py — queue-based pause gate*
# Each stuck DM sequence registers its own private asyncio.Event.
# /resume pops the oldest waiter (FIFO), sleeps POST_RESUME_DELAY,
# then fires only that one Event. Fully serialized — no simultaneous
# Discord hits, no cross-wakeup between unrelated sequences.

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


POST_RESUME_DELAY: float = 30.0     # seconds to wait after /resume before retrying


@dataclass
class Waiter:
    """One stuck DM sequence waiting for manual resume."""
    user_id:     int
    username:    str
    part_index:  int          # 0-based
    total_parts: int
    last_error:  str
    queued_at:   float = field(default_factory=time.time)
    _event:      asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self) -> None:
        """Block the DM sequence here until resume() fires us."""
        await self._event.wait()

    def resume(self) -> None:
        """Wake this specific sequence."""
        self._event.set()


class CaptchaQueue:
    """
    FIFO queue of Waiter objects.

    dm_sender  → queue.add(waiter)       register a stuck sequence
    telegram   → queue.resume_next()     pop oldest, sleep, fire it
    telegram   → queue.status()          snapshot for /status command
    """

    def __init__(self) -> None:
        self._q: deque[Waiter] = deque()
        self._lock = asyncio.Lock()

    @property
    def pending(self) -> int:
        return len(self._q)

    @property
    def is_paused(self) -> bool:
        return len(self._q) > 0

    def add(self, waiter: Waiter) -> None:
        """Register a new stuck sequence. Called from dm_sender (sync-safe)."""
        self._q.append(waiter)

    async def resume_next(self) -> Waiter | None:
        """
        Pop the oldest waiter, sleep POST_RESUME_DELAY, fire its event.
        Returns the Waiter that was resumed, or None if queue was empty.
        """
        async with self._lock:
            if not self._q:
                return None
            waiter = self._q.popleft()

        # Sleep outside the lock so other operations aren't blocked
        if POST_RESUME_DELAY > 0:
            await asyncio.sleep(POST_RESUME_DELAY)

        waiter.resume()
        return waiter

    def status(self) -> list[Waiter]:
        """Return a snapshot of all pending waiters in queue order."""
        return list(self._q)


# ── singleton ──────────────────────────────────────────────────────────────
captcha_queue = CaptchaQueue()