# [context: discord-selfbot-monitor, os: linux, arch: x86_64]
import asyncio
import logging
import random
import re
import time

import discord
from tg.state import POST_RESUME_DELAY, Waiter

log = logging.getLogger("dm_sender")

# Generic "include / end with this word" instructions in request embeds
_INCLUDE_WORD_RE = re.compile(
    r"(?:also\s+)?include\s+(?:the\s+)?word\s+[\"\']?([A-Za-z0-9_\-]+)[\"\']?",
    re.IGNORECASE,
)
_END_WITH_WORD_RE = re.compile(
    r"end\s+(?:your\s+)?message\s+with\s+(?:the\s+)?word\s+[\"\']?([A-Za-z0-9_\-]+)[\"\']?",
    re.IGNORECASE,
)
# fallback: "end your message with X" / "end with X in uppercase"
_END_WITH_LOOSE_RE = re.compile(
    r"end\s+(?:your\s+)?(?:message\s+)?with\s+[\"\']?([A-Za-z0-9_\-]+)[\"\']?"
    r"(?:\s+in\s+uppercase)?",
    re.IGNORECASE,
)


class DMSender:
    def __init__(self, settings, storage, captcha_queue, notify_captcha_cb=None):
        self.settings = settings
        self.storage = storage
        self.captcha_queue = captcha_queue
        self.notify_captcha_cb = notify_captcha_cb
        self._seq_index = 0
        # persist sequential cursor across restarts via storage if possible
        self._load_seq_index()

    def _load_seq_index(self):
        try:
            # optional table; ignore if not present
            with self.storage._lock:
                row = self.storage._conn.execute(
                    "SELECT value FROM meta WHERE key = 'dm_seq_index'"
                ).fetchone()
            if row is not None:
                self._seq_index = int(row[0])
        except Exception:
            self._seq_index = 0

    def _save_seq_index(self):
        try:
            with self.storage._lock:
                self.storage._conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta ("
                    "key TEXT PRIMARY KEY, value TEXT)"
                )
                self.storage._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("dm_seq_index", str(self._seq_index)),
                )
                self.storage._conn.commit()
        except Exception as exc:
            log.debug("[DM] could not persist seq index: %s", exc)

    def pick_message(self):
        messages = list(self.settings.dm_messages or [])
        if not messages:
            return None, -1

        rotation = (self.settings.dm_rotation or "random").strip().lower()
        n = len(messages)

        if rotation == "sequential":
            idx = self._seq_index % n
            self._seq_index = (self._seq_index + 1) % n
            self._save_seq_index()
            log.info(
                "[DM] sequential pick index=%d/%d next_cursor=%d",
                idx,
                n,
                self._seq_index,
            )
            return messages[idx], idx

        # random (default) — uniform across the whole pool every time
        idx = random.randrange(n)
        log.info("[DM] random pick index=%d/%d pool_size=%d", idx, n, n)
        return messages[idx], idx

    def _split_parts(self, raw):
        text = raw.replace("\\n", "\n").replace("\r\n", "\n")
        parts = [p.strip() for p in text.split("\n")]
        return [p for p in parts if p]

    def _extract_challenge_words(self, trigger_text: str) -> list:
        """
        Parse the request text for instructions like:
          - Also include the word "Bloop"
          - end your message with the word "MEGALODON" in uppercase
          - include the word best
        Returns ordered unique words to append (already cased as requested).
        """
        if not trigger_text:
            return []

        found = []
        seen = set()

        def add(word: str, force_upper: bool = False):
            if not word:
                return
            w = word.upper() if force_upper else word
            key = w.lower()
            if key in seen:
                return
            seen.add(key)
            found.append(w)

        # "include the word X"
        for m in _INCLUDE_WORD_RE.finditer(trigger_text):
            add(m.group(1))

        # "end your message with the word X" / "… in uppercase"
        for m in _END_WITH_WORD_RE.finditer(trigger_text):
            word = m.group(1)
            span = trigger_text[m.start(): m.end() + 20].lower()
            force_upper = "uppercase" in span or "upper case" in span
            # if the quoted/captured word is already all-caps in the source, keep it
            if word.isupper():
                force_upper = True
            add(word, force_upper=force_upper)

        # loose "end with X" if nothing found yet
        if not found:
            for m in _END_WITH_LOOSE_RE.finditer(trigger_text):
                word = m.group(1)
                # skip common filler words that aren't challenges
                if word.lower() in {"the", "a", "an", "your", "message", "word"}:
                    continue
                span = trigger_text[m.start(): m.end() + 20].lower()
                force_upper = "uppercase" in span or word.isupper()
                add(word, force_upper=force_upper)

        return found

    def _apply_challenge_suffix(self, parts, challenge_words: list):
        """
        Send required challenge words as an EXTRA final DM (new message),
        not glued onto the previous part.
        """
        if not parts or not challenge_words:
            return parts

        # skip words already present in any existing part
        existing = " ".join(parts).lower()
        to_add = [w for w in challenge_words if w.lower() not in existing]
        if not to_add:
            return parts

        # each required word (or the whole set) as its own trailing message
        # one extra part containing all required words keeps part count sane
        extra = " ".join(to_add)
        parts = list(parts) + [extra]
        log.info("[DM] will send challenge words as extra final message: %r", extra)
        return parts

    async def send_dm_sequence(
        self, user, trigger_author, session_key, trigger_text=""
    ):
        message, pool_index = self.pick_message()
        if message is None:
            log.warning("[DM] no DM messages configured")
            return False

        parts = self._split_parts(message)
        if not parts:
            log.warning("[DM] message #%d contained no parts", pool_index)
            return False

        challenge_words = self._extract_challenge_words(trigger_text or "")
        if challenge_words:
            log.info("[DM] challenge words detected: %s", challenge_words)
            parts = self._apply_challenge_suffix(parts, challenge_words)

        initial_delay = random.uniform(
            self.settings.dm_delay_min_seconds,
            self.settings.dm_delay_max_seconds,
        )
        log.info(
            "[DM] ┌─ DM sequence start\n"
            "[DM] │ timestamp=%s\n"
            "[DM] │ author=%s\n"
            "[DM] │ user=%s (%s)\n"
            "[DM] │ parts=%d pool_index=%d rotation=%s",
            time.strftime("%Y-%m-%d %H:%M:%S"),
            trigger_author,
            user.name,
            user.id,
            len(parts),
            pool_index,
            self.settings.dm_rotation,
        )
        await asyncio.sleep(initial_delay)

        try:
            for i, part in enumerate(parts):
                ok = await self._send_part(user, part, i, trigger_author)
                if not ok:
                    self.storage.complete_dm(user.id, session_key, False)
                    log.info("[DM] └─ DM sequence aborted user=%s", user.name)
                    return False
                if i < len(parts) - 1:
                    delay = random.uniform(
                        self.settings.part_delay_min_seconds,
                        self.settings.part_delay_max_seconds,
                    )
                    await asyncio.sleep(delay)
        except Exception as exc:
            self.storage.complete_dm(user.id, session_key, False)
            log.error(
                "[DM] └─ DM sequence failed user=%s exc=%s", user.name, exc
            )
            return False

        self.storage.complete_dm(user.id, session_key, True)
        self.storage.record_dm(user.id)
        self.storage.log_sent(user.id, user.name, trigger_author, pool_index)
        log.info(
            "[DM] └─ DM sequence complete user=%s pool_index=%d",
            user.name,
            pool_index,
        )
        return True

    async def _send_part(self, user, content, part_index, trigger_author):
        while True:
            try:
                await user.send(content)
                log.info(
                    "[DM] part %d sent user=%s content_len=%d pool_preview=%r",
                    part_index + 1,
                    user.name,
                    len(content),
                    content[:40],
                )
                return True
            except discord.HTTPException as exc:
                text = str(exc).lower()
                if "captcha" in text or getattr(exc, "code", None) == -1:
                    waiter = Waiter(
                        user.id, user.name, part_index + 1, trigger_author
                    )
                    self.captcha_queue.push(waiter)
                    log.warning(
                        "[DM] captcha hit user=%s part=%d queue_size=%d",
                        user.name,
                        part_index + 1,
                        len(self.captcha_queue),
                    )
                    if self.notify_captcha_cb is not None:
                        try:
                            await self.notify_captcha_cb(waiter)
                        except Exception as exc_notify:
                            log.error(
                                "[DM] telegram captcha notify failed: %s",
                                exc_notify,
                            )
                    await waiter.wait()
                    if waiter.skipped:
                        return False
                    await asyncio.sleep(POST_RESUME_DELAY)
                    continue
                log.error(
                    "[DM] HTTPException user=%s part=%d code=%s text=%s",
                    user.name,
                    part_index + 1,
                    getattr(exc, "code", None),
                    exc,
                )
                return False
