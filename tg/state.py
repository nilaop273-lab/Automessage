from __future__ import annotations

# *state.py — queue-based pause gate*
# Each stuck DM sequence registers its own private asyncio.Event.
# /resume pops the oldest waiter (FIFO), sleeps POST_RESUME_DELAY,
# then fires only that one Event. Fully serialized — no simultaneous
# Discord hits, no cross-wakeup between unrelated sequences.
#
# /skip fires the waiter's event too BUT sets waiter.skipped = True
# so dm_sender knows to abort the sequence instead of retrying.
# Without firing the event the frozen coroutine would hang forever —
# the skipped flag is what separates "retry" from "abort".

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


POST_RESUME_DELAY: float = 10.0     # seconds to wait after /resume before retrying


@dataclass
class Waiter:
    """One stuck DM sequence waiting for manual resume or skip."""
    user_id:     int
    username:    str
    part_index:  int            # 0-based
    total_parts: int
    last_error:  str
    queued_at:   float        = field(default_factory=time.time)
    skipped:     bool         = field(default=False)
    _event:      asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self) -> None:
        """Block the DM sequence here until resume() or skip() fires us."""
        await self._event.wait()

    def resume(self) -> None:
        """Wake this sequence for a retry."""
        self.skipped = False
        self._event.set()

    def abort(self) -> None:
        """
        Wake this sequence but mark it as skipped so dm_sender aborts
        instead of retrying. Must be called — never leave the event unset
        or the frozen coroutine leaks forever.
        """
        self.skipped = True
        self._event.set()


class CaptchaQueue:
    """
    FIFO queue of Waiter objects.

    dm_sender  → queue.add(waiter)       register a stuck sequence
    telegram   → queue.resume_next()     pop oldest, sleep, fire it
    telegram   → queue.skip(position)    remove by position, abort it
    telegram   → queue.status()          snapshot for /queue command
    """

    def __init__(self) -> None:
        self._q:    deque[Waiter] = deque()
        self._lock: asyncio.Lock  = asyncio.Lock()

    @property
    def pending(self) -> int:
        return len(self._q)

    @property
    def is_paused(self) -> bool:
        return len(self._q) > 0

    def add(self, waiter: Waiter) -> None:
        """Register a new stuck sequence. Called from dm_sender."""
        self._q.append(waiter)

    async def resume_next(self) -> Waiter | None:
        """
        Pop the oldest waiter, sleep POST_RESUME_DELAY, fire its event.
        Returns the resumed Waiter, or None if queue was empty.
        """
        async with self._lock:
            if not self._q:
                return None
            waiter = self._q.popleft()

        # Sleep outside the lock so skip() can still run concurrently
        if POST_RESUME_DELAY > 0:
            await asyncio.sleep(POST_RESUME_DELAY)

        waiter.resume()
        return waiter

    def skip(self, position: int) -> Waiter | None:
        """
        Remove waiter at 1-based position and abort its frozen coroutine.

        Fires the waiter's event with skipped=True so dm_sender wakes up
        and returns False (abort) instead of retrying the send.

        Returns the skipped Waiter, or None if position is out of range.
        """
        idx = position - 1
        if idx < 0 or idx >= len(self._q):
            return None

        waiter = self._q[idx]
        del self._q[idx]

        # MUST fire the event — otherwise the coroutine blocks forever
        waiter.abort()
        return waiter

    def status(self) -> list[Waiter]:
        """Snapshot of all pending waiters in queue order."""
        return list(self._q)


# ── singleton ──────────────────────────────────────────────────────────────
captcha_queue = CaptchaQueue()
