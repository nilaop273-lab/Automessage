# [context: discord-selfbot-monitor]
"""
NoneCap hCaptcha solver + Discord captcha payload parser.

Used when Discord returns a captcha challenge on DM send.
On success returns a token to put in X-Captcha-Key (plus optional
rqtoken / session headers). On any failure returns None so the caller
can fall back to the existing Telegram /resume path.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger("captcha_solver")

NONECAP_BASE = "https://api.nonecap.com/v1"
DEFAULT_PAGE_URL = "https://discord.com/channels/@me"
# Discord often uses this sitekey for client challenges
DISCORD_DEFAULT_SITEKEY = "a9b5fb07-92ff-493f-86fe-352a2803b3df"


@dataclass
class CaptchaChallenge:
    sitekey: str
    service: str = "hcaptcha"
    rqdata: str | None = None
    rqtoken: str | None = None
    session_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class CaptchaSolution:
    token: str
    rqtoken: str | None = None
    session_id: str | None = None


def _coerce_mapping(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = obj.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            # try to find an embedded JSON object
            m = re.search(r"\{[^{}]*captcha_sitekey[^{}]*\}", text, re.DOTALL)
            if not m:
                m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def parse_captcha_from_exception(exc: BaseException) -> CaptchaChallenge | None:
    """
    Pull captcha fields out of a discord.HTTPException (or similar).
    discord.py-self usually puts the API body on .text; some builds expose
    .response / .code. We try several places and also regex the string form.
    """
    candidates: list[Any] = []

    for attr in ("text", "response", "body", "payload"):
        if hasattr(exc, attr):
            candidates.append(getattr(exc, attr))

    # aiohttp-style response on some wrappers
    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("data", "text", "_body"):
            if hasattr(resp, attr):
                candidates.append(getattr(resp, attr))

    candidates.append(str(exc))

    data: dict[str, Any] | None = None
    for c in candidates:
        data = _coerce_mapping(c)
        if data and ("captcha_sitekey" in data or "captcha_key" in data):
            break
        data = None

    if not data:
        # last-chance regex on the full exception string
        s = str(exc)
        sk = re.search(
            r'captcha_sitekey["\']?\s*[:=]\s*["\']([0-9a-fA-F-]{8,})["\']', s
        )
        if not sk:
            return None
        sitekey = sk.group(1)
        rqdata_m = re.search(
            r'captcha_rqdata["\']?\s*[:=]\s*["\']([^"\']+)["\']', s
        )
        rqtoken_m = re.search(
            r'captcha_rqtoken["\']?\s*[:=]\s*["\']([^"\']+)["\']', s
        )
        sess_m = re.search(
            r'captcha_session_id["\']?\s*[:=]\s*["\']([^"\']+)["\']', s
        )
        return CaptchaChallenge(
            sitekey=sitekey,
            rqdata=rqdata_m.group(1) if rqdata_m else None,
            rqtoken=rqtoken_m.group(1) if rqtoken_m else None,
            session_id=sess_m.group(1) if sess_m else None,
        )

    sitekey = data.get("captcha_sitekey") or data.get("sitekey")
    if not sitekey:
        return None

    return CaptchaChallenge(
        sitekey=str(sitekey),
        service=str(data.get("captcha_service") or "hcaptcha"),
        rqdata=data.get("captcha_rqdata") or data.get("rqdata"),
        rqtoken=data.get("captcha_rqtoken") or data.get("rqtoken"),
        session_id=data.get("captcha_session_id") or data.get("session_id"),
        raw=data,
    )


async def solve_with_nonecap(
    api_key: str,
    challenge: CaptchaChallenge,
    *,
    page_url: str = DEFAULT_PAGE_URL,
    wait_seconds: int = 90,
    session: aiohttp.ClientSession | None = None,
) -> CaptchaSolution | None:
    """
    Call NoneCap Token API. Returns CaptchaSolution or None on any failure.
    Failed solves are not charged by NoneCap.
    """
    if not api_key:
        return None

    # Prefer enterprise type when Discord sent rqdata
    captcha_type = "hcaptcha_enterprise" if challenge.rqdata else "hcaptcha"

    body: dict[str, Any] = {
        "type": captcha_type,
        "sitekey": challenge.sitekey or DISCORD_DEFAULT_SITEKEY,
        "url": page_url,
    }
    if challenge.rqdata:
        # NoneCap accepts enterprise rqdata on the body for hcaptcha_enterprise
        body["rqdata"] = challenge.rqdata

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    params = {"wait": max(1, min(int(wait_seconds), 90))}

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    assert session is not None
    try:
        log.info(
            "[CAPTCHA] NoneCap solve type=%s sitekey=%s… rqdata=%s",
            captcha_type,
            (challenge.sitekey or "")[:12],
            "yes" if challenge.rqdata else "no",
        )
        async with session.post(
            f"{NONECAP_BASE}/solves",
            headers=headers,
            params=params,
            json=body,
            timeout=aiohttp.ClientTimeout(total=wait_seconds + 30),
        ) as resp:
            payload = await resp.json(content_type=None)
            status = payload.get("status")
            token = payload.get("token")

            if resp.status == 202 or (status not in ("solved",) and not token):
                # poll a few times
                solve_id = payload.get("id")
                if not solve_id:
                    log.warning("[CAPTCHA] NoneCap no id to poll: %s", payload)
                    return None
                token = await _poll_solve(
                    session, api_key, solve_id, wait_seconds=wait_seconds
                )
            elif status == "solved" and token:
                pass
            else:
                log.warning(
                    "[CAPTCHA] NoneCap failed status=%s http=%s body=%s",
                    status,
                    resp.status,
                    {k: payload.get(k) for k in ("status", "error", "message", "id")},
                )
                return None

        if not token:
            return None

        log.info("[CAPTCHA] NoneCap solved token_len=%d", len(token))
        return CaptchaSolution(
            token=token,
            rqtoken=challenge.rqtoken,
            session_id=challenge.session_id,
        )
    except Exception as exc:
        log.warning("[CAPTCHA] NoneCap exception: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        if owns_session:
            await session.close()


async def _poll_solve(
    session: aiohttp.ClientSession,
    api_key: str,
    solve_id: str,
    *,
    wait_seconds: int = 90,
) -> str | None:
    headers = {"Authorization": f"Bearer {api_key}"}
    # a few long-polls
    remaining = wait_seconds
    while remaining > 0:
        chunk = min(30, remaining)
        try:
            async with session.get(
                f"{NONECAP_BASE}/solves/{solve_id}",
                headers=headers,
                params={"wait": chunk},
                timeout=aiohttp.ClientTimeout(total=chunk + 15),
            ) as resp:
                payload = await resp.json(content_type=None)
                if payload.get("status") == "solved" and payload.get("token"):
                    return payload["token"]
                if payload.get("status") in ("failed", "cancelled", "expired"):
                    log.warning("[CAPTCHA] NoneCap poll terminal: %s", payload.get("status"))
                    return None
        except Exception as exc:
            log.warning("[CAPTCHA] NoneCap poll error: %s", exc)
            return None
        remaining -= chunk
    return None


async def send_dm_with_captcha(
    user_token: str,
    recipient_id: int,
    content: str,
    solution: CaptchaSolution,
    *,
    session: aiohttp.ClientSession | None = None,
) -> tuple[bool, str]:
    """
    Raw Discord REST: open (or reuse) DM channel, send message with captcha headers.
    Returns (ok, detail).
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    assert session is not None

    api = "https://discord.com/api/v9"
    headers = {
        "Authorization": user_token,
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "X-Captcha-Key": solution.token,
    }
    if solution.rqtoken:
        headers["X-Captcha-Rqtoken"] = solution.rqtoken
    if solution.session_id:
        headers["X-Captcha-Session-Id"] = solution.session_id

    try:
        # 1) open / get DM channel
        async with session.post(
            f"{api}/users/@me/channels",
            headers=headers,
            json={"recipient_id": str(recipient_id)},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            ch_body = await resp.json(content_type=None)
            if resp.status not in (200, 201):
                # still try if channel id present
                if "id" not in ch_body:
                    return False, f"open_dm http={resp.status} body={ch_body}"
            channel_id = ch_body.get("id")
            if not channel_id:
                return False, f"open_dm missing channel id: {ch_body}"

        # 2) send message
        async with session.post(
            f"{api}/channels/{channel_id}/messages",
            headers=headers,
            json={"content": content},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status in (200, 201):
                return True, "ok"
            return False, f"send http={resp.status} body={body}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if owns_session:
            await session.close()
