# [context: discord-selfbot-monitor, os: linux, arch: x86_64]
import logging
import discord
from monitor.handlers import handle_message
from monitor.presence import PresenceManager

log = logging.getLogger("monitor")


def _extract_from_components(components):
    """Recursively pull readable text out of Components V2 (and legacy)."""
    texts = []
    if not components:
        return texts

    def walk(comp):
        if comp is None:
            return

        # dict form (raw API payload)
        if isinstance(comp, dict):
            t = comp.get("type")
            # type 10 = Text Display (Components V2)
            if t == 10:
                c = comp.get("content")
                if c and isinstance(c, str) and c.strip():
                    texts.append(c)
            for key in ("components", "children", "items"):
                for child in comp.get(key) or []:
                    walk(child)
            acc = comp.get("accessory")
            if acc:
                walk(acc)
            for key in ("label", "placeholder", "description", "title", "content"):
                val = comp.get(key)
                if isinstance(val, str) and val.strip() and val not in texts:
                    texts.append(val)
            return

        # object form (discord.py-self MessageComponent subclasses)
        content = getattr(comp, "content", None)
        if content and isinstance(content, str) and content.strip():
            texts.append(content)

        for child in (
            getattr(comp, "children", None)
            or getattr(comp, "components", None)
            or []
        ):
            walk(child)

        acc = getattr(comp, "accessory", None)
        if acc is not None:
            walk(acc)

        for attr in ("label", "placeholder", "description", "title"):
            val = getattr(comp, attr, None)
            if isinstance(val, str) and val.strip() and val not in texts:
                texts.append(val)

    for c in components:
        walk(c)
    return texts


def _text_from_raw_payload(raw: dict) -> str:
    """Pull every usable string from a raw message dict returned by HTTP."""
    if not isinstance(raw, dict):
        return ""
    parts = []

    content = raw.get("content") or ""
    if content.strip():
        parts.append(content)

    for emb in raw.get("embeds") or []:
        if not isinstance(emb, dict):
            continue
        for key in ("title", "description"):
            val = emb.get(key)
            if val and isinstance(val, str) and val.strip():
                parts.append(val)
        author = emb.get("author") or {}
        if isinstance(author, dict) and author.get("name"):
            parts.append(str(author["name"]))
        footer = emb.get("footer") or {}
        if isinstance(footer, dict) and footer.get("text"):
            parts.append(str(footer["text"]))
        for field in emb.get("fields") or []:
            if not isinstance(field, dict):
                continue
            name = field.get("name") or ""
            value = field.get("value") or ""
            if name or value:
                parts.append(f"{name} {value}".strip())

    # Components V2 + legacy
    comp_texts = _extract_from_components(raw.get("components") or [])
    parts.extend(comp_texts)

    # interaction / message metadata sometimes carries the trigger text
    interaction = raw.get("interaction") or {}
    if isinstance(interaction, dict):
        name = interaction.get("name")
        if name:
            parts.append(str(name))

    return "\n".join(p for p in parts if p and str(p).strip())


class MonitorClient(discord.Client):
    def __init__(self, settings, storage, dm_sender=None, presence=None):
        super().__init__()
        self.cfg = settings
        self._storage = storage
        self._dm_sender = dm_sender
        self.presence = presence or PresenceManager()
        self.presence.attach(self)

    async def on_ready(self):
        user = self.user
        name = user.name if user else "unknown"
        uid = user.id if user else "?"
        banner = (
            "┌─ SELF BOT STARTED ─\n"
            f"│ account={name} ({uid})\n"
            f"│ channel={self.cfg.paid_request_channel_id}\n"
            f"│ trigger_author={self.cfg.paid_request_trigger_author}\n"
            f"│ cooldown={self.cfg.dm_cooldown_seconds}s\n"
            f"│ initial_delay={self.cfg.dm_delay_min_seconds}-"
            f"{self.cfg.dm_delay_max_seconds}s\n"
            f"│ part_delay={self.cfg.part_delay_min_seconds}-"
            f"{self.cfg.part_delay_max_seconds}s\n"
            f"│ pool_size={len(self.cfg.dm_messages)}\n"
            "└─ READY"
        )
        log.info("[PAID] %s", banner)
        # apply custom presence if configured
        try:
            import asyncio
            asyncio.create_task(self.presence.apply())
        except Exception as exc:
            log.debug("[PAID] presence apply on_ready failed: %s", exc)

    async def _raw_get_message(self, channel_id: int, message_id: int):
        """Bypass library parsing — hit the REST endpoint directly."""
        try:
            # discord.py-self exposes the low-level HTTP client on the connection
            data = await self.http.get_message(channel_id, message_id)
            return data
        except Exception as exc:
            log.error(
                "[PAID] raw http.get_message failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None

    async def on_message(self, message):
        if self.user is None or message.author.id == self.user.id:
            return
        if message.channel.id != self.cfg.paid_request_channel_id:
            return

        content = getattr(message, "content", "") or ""
        embeds = getattr(message, "embeds", []) or []
        components = getattr(message, "components", None) or []

        log.info(
            "[PAID] saw message in channel "
            "message_id=%s author_name=%r display_name=%r "
            "global_name=%r bot=%s content=%r embeds=%d components=%d",
            message.id,
            getattr(message.author, "name", None),
            getattr(message.author, "display_name", None),
            getattr(message.author, "global_name", None),
            getattr(message.author, "bot", False),
            content,
            len(embeds),
            len(components),
        )

        # 1) Normal library fetch (still useful when it works)
        if getattr(message.author, "bot", False) and (
            not content and not embeds and not components
        ):
            try:
                fetched = await message.channel.fetch_message(message.id)
                if fetched:
                    content = getattr(fetched, "content", "") or ""
                    embeds = getattr(fetched, "embeds", []) or []
                    components = getattr(fetched, "components", None) or []
                    message = fetched
                    log.info(
                        "[PAID] fetched bot message via HTTP "
                        "message_id=%s content=%r embeds=%d components=%d",
                        fetched.id,
                        content,
                        len(embeds),
                        len(components),
                    )
            except Exception as exc:
                log.error(
                    "[PAID] HTTP fetch for bot message failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        # 2) Component walk on whatever the library gave us
        comp_texts = _extract_from_components(components)
        if comp_texts:
            joined = "\n".join(comp_texts)
            log.info(
                "[PAID] extracted %d component text pieces (len=%d)",
                len(comp_texts),
                len(joined),
            )
            content = (content + "\n" + joined).strip() if content else joined

        # 3) CRITICAL: raw REST payload — library often strips Components V2
        if not content:
            raw = await self._raw_get_message(message.channel.id, message.id)
            if raw is not None:
                # log a short summary so we can see what Discord actually sent
                flags = raw.get("flags", 0)
                raw_comps = raw.get("components") or []
                log.info(
                    "[PAID] raw payload flags=%s components=%d content_len=%d",
                    flags,
                    len(raw_comps),
                    len(raw.get("content") or ""),
                )
                # dump first 1500 chars of the raw dict for debugging (one time)
                try:
                    import json
                    dump = json.dumps(raw, ensure_ascii=False, default=str)
                    log.info("[PAID] raw dump (truncated): %s", dump[:1500])
                except Exception:
                    pass

                extracted = _text_from_raw_payload(raw)
                if extracted:
                    content = extracted
                    log.info(
                        "[PAID] extracted from raw payload len=%d preview=%r",
                        len(content),
                        content[:200],
                    )
                else:
                    log.warning(
                        "[PAID] raw payload also empty of usable text. "
                        "keys=%s",
                        list(raw.keys()) if isinstance(raw, dict) else type(raw),
                    )

        # 4) Last-resort: referenced / replied-to message
        if not content and not embeds:
            referenced = None
            ref = getattr(message, "reference", None)
            if ref is not None:
                ref_id = getattr(ref, "message_id", None)
                if ref_id:
                    try:
                        referenced = await message.channel.fetch_message(ref_id)
                    except Exception as exc:
                        log.error(
                            "[PAID] fetch referenced failed: %s: %s",
                            type(exc).__name__,
                            exc,
                        )
            if referenced is not None:
                ref_content = getattr(referenced, "content", "") or ""
                ref_embeds = getattr(referenced, "embeds", []) or []
                ref_comps = getattr(referenced, "components", None) or []
                ref_comp_texts = _extract_from_components(ref_comps)
                log.info(
                    "[PAID] referenced message found ref_id=%s content=%r "
                    "embeds=%d component_texts=%d",
                    referenced.id,
                    ref_content,
                    len(ref_embeds),
                    len(ref_comp_texts),
                )
                combined = ref_content
                if not combined and ref_comp_texts:
                    combined = "\n".join(ref_comp_texts)
                if not combined:
                    buf = []
                    for e in ref_embeds:
                        t = getattr(e, "title", None)
                        d = getattr(e, "description", None)
                        if t:
                            buf.append(t)
                        if d:
                            buf.append(d)
                        for f in getattr(e, "fields", []) or []:
                            buf.append(
                                f"{getattr(f, 'name', '')} {getattr(f, 'value', '')}"
                            )
                    combined = "\n".join(buf)
                # also try raw for the referenced message
                if not combined:
                    raw_ref = await self._raw_get_message(
                        message.channel.id, referenced.id
                    )
                    if raw_ref:
                        combined = _text_from_raw_payload(raw_ref)
                content = combined
                embeds = ref_embeds
            else:
                log.warning(
                    "[PAID] message empty and no referenced text / no components."
                )

        if self._dm_sender is None:
            log.error("dm_sender not set — ignoring message")
            return

        await handle_message(
            message,
            self.cfg,
            self._storage,
            self._dm_sender,
            self,
            content_override=content,
        )
