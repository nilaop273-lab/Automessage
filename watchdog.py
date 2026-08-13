from __future__ import annotations

# *watchdog.py — always-on Telegram process manager*
#
# Always running. Spawns/kills main.py on demand.
# Sets WATCHDOG_MODE=true before spawning so main.py skips its own
# Telegram poll loop — fixing the 409 Conflict error.
#
# Commands handled HERE (watchdog owns the poll loop):
#   /start   → spawn main.py
#   /stop    → kill main.py gracefully
#   /status  → bot process state + uptime
#   /resume  → forwarded to main.py via telegram_bot.notify_action()
#   /queue   → forwarded to main.py
#   /logs    → forwarded to main.py
#   /help    → full command list
#
# Run: python watchdog.py

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── config ──────────────────────────────────────────────────────────────────
TG_BOT_TOKEN: str  = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID:   int  = int(os.getenv("TG_CHAT_ID", "0"))
MAIN_SCRIPT:  Path = Path(__file__).resolve().parent / "main.py"
PYTHON_BIN:   str  = sys.executable

# ── constants ───────────────────────────────────────────────────────────────
_POLL_TIMEOUT = 30
_BASE         = "https://api.telegram.org/bot{token}/{method}"
_MAX_CHARS    = 3800

# ── process state ────────────────────────────────────────────────────────────
_proc:            subprocess.Popen | None = None
_started_at:      float | None           = None
_log_file_handle: object                  = None   # open file or None


# ── Telegram helpers ─────────────────────────────────────────────────────────

def _url(method: str) -> str:
    return _BASE.format(token=TG_BOT_TOKEN, method=method)


async def _send(session: aiohttp.ClientSession, text: str) -> None:
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n…(truncated)"
    try:
        async with session.post(
            _url("sendMessage"),
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("sendMessage failed %d: %s", resp.status, body)
    except Exception as exc:
        logger.error("sendMessage exception: %s: %s", type(exc).__name__, exc)


async def _get_updates(session: aiohttp.ClientSession, offset: int) -> list[dict]:
    try:
        async with session.get(
            _url("getUpdates"),
            params={"timeout": _POLL_TIMEOUT, "offset": offset},
            timeout=aiohttp.ClientTimeout(total=_POLL_TIMEOUT + 10),
        ) as resp:
            if resp.status == 409:
                # Two pollers on the same token — should never happen now
                # since main.py skips its loop in WATCHDOG_MODE
                logger.error(
                    "409 Conflict — another process is polling this token. "
                    "Make sure main.py is always started via watchdog, not directly."
                )
                await asyncio.sleep(5)
                return []
            if resp.status != 200:
                logger.warning("getUpdates returned %d", resp.status)
                return []
            data = await resp.json()
            return data.get("result", [])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("getUpdates exception: %s: %s", type(exc).__name__, exc)
        return []


async def _drain_stale(session: aiohttp.ClientSession) -> int:
    try:
        async with session.get(
            _url("getUpdates"),
            params={"timeout": 0},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data    = await resp.json()
                results = data.get("result", [])
                if results:
                    off = results[-1]["update_id"] + 1
                    logger.info("Drained %d stale update(s)", len(results))
                    return off
    except Exception as exc:
        logger.warning("Drain failed: %s: %s", type(exc).__name__, exc)
    return 0


# ── process helpers ───────────────────────────────────────────────────────────

def _is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def _uptime() -> str:
    if _started_at is None:
        return "unknown"
    secs       = int(time.time() - _started_at)
    h, rem     = divmod(secs, 3600)
    m, s       = divmod(rem, 60)
    if h:   return f"{h}h {m}m {s}s"
    if m:   return f"{m}m {s}s"
    return f"{s}s"


def _start_bot() -> tuple[bool, str]:
    global _proc, _started_at, _log_file_handle

    if _is_running():
        return False, f"⚠️ Bot already running  (pid: {_proc.pid})"  # type: ignore[union-attr]
    if not MAIN_SCRIPT.exists():
        return False, f"❌ main.py not found at {MAIN_SCRIPT}"

    # ── env: tell main.py to skip its Telegram poll loop ──────────────────
    env = os.environ.copy()
    env["WATCHDOG_MODE"] = "true"

    # ── log file: timestamped, sits next to watchdog.py ───────────────────
    log_path = Path(__file__).resolve().parent / f"bot_{int(time.time())}.log"
    try:
        _log_file_handle = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: WPS515
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", log_path, exc)
        _log_file_handle = None

    # stdout=None  → main.py inherits watchdog's terminal → banner visible
    # If we have a log file we can't do both at once without a thread/tee,
    # so we choose: log file when available, terminal fallback otherwise.
    out = _log_file_handle if _log_file_handle else None
    err = _log_file_handle if _log_file_handle else None

    try:
        _proc = subprocess.Popen(
            [PYTHON_BIN, "-u", str(MAIN_SCRIPT)],   # -u = unbuffered so lines appear immediately
            env=env,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        _started_at = time.time()
        log_note = f"  log → {log_path.name}" if _log_file_handle else "  (no log file)"
        logger.info("Started main.py  (pid: %d)%s", _proc.pid, log_note)
        return (
            True,
            f"✅ Bot started  (pid: {_proc.pid})\n"
            f"📄 Logs: {log_path.name if _log_file_handle else 'terminal only'}\n"
            f"💡 Tip: tail -f {log_path.name} to follow live"
            if _log_file_handle else
            f"✅ Bot started  (pid: {_proc.pid})"
        )
    except Exception as exc:
        return False, f"❌ Failed to start: {exc}"


async def _stop_bot(session: aiohttp.ClientSession) -> tuple[bool, str]:
    global _proc, _started_at

    if not _is_running():
        _proc       = None
        _started_at = None
        return False, "⚠️ Bot is not running."

    pid = _proc.pid  # type: ignore[union-attr]
    logger.info("Sending SIGTERM to main.py  (pid: %d)", pid)

    try:
        if sys.platform == "win32":
            _proc.terminate()  # type: ignore[union-attr]
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        _proc       = None
        _started_at = None
        return True, "✅ Bot was already dead — cleaned up."
    except Exception as exc:
        return False, f"❌ SIGTERM failed: {exc}"

    await _send(session, f"⏳ Stopping bot  (pid: {pid}) — waiting up to 8s…")

    for _ in range(16):      # 16 × 0.5s = 8s
        await asyncio.sleep(0.5)
        if not _is_running():
            break
    else:
        logger.warning("main.py still alive after SIGTERM — sending SIGKILL")
        try:
            if sys.platform == "win32":
                _proc.kill()  # type: ignore[union-attr]
            else:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
        await asyncio.sleep(1)

    _proc       = None
    _started_at = None
    if _log_file_handle is not None:
        try:
            _log_file_handle.close()  # type: ignore[union-attr]
        except Exception:
            pass
        globals()["_log_file_handle"] = None
    logger.info("main.py stopped  (pid: %d)", pid)
    return True, f"🛑 Bot stopped  (pid: {pid})"


# ── command handlers ───────────────────────────────────────────────────────────

async def _cmd_start(session: aiohttp.ClientSession) -> None:
    ok, msg = _start_bot()
    await _send(session, msg)


async def _cmd_stop(session: aiohttp.ClientSession) -> None:
    ok, msg = await _stop_bot(session)
    await _send(session, msg)


async def _cmd_status(session: aiohttp.ClientSession) -> None:
    if _is_running():
        assert _proc is not None
        bot_line   = f"🤖 Bot: RUNNING\n   pid: {_proc.pid}  •  uptime: {_uptime()}"
        queue_line = "📋 Captcha queue: send /queue to check"
    else:
        bot_line   = "🤖 Bot: STOPPED"
        queue_line = "📋 Captcha queue: N/A  (bot offline)"

    await _send(session, f"{bot_line}\n{queue_line}")


async def _cmd_help(session: aiohttp.ClientSession) -> None:
    await _send(
        session,
        (
            "📖  COMMAND LIST\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "🔧  PROCESS CONTROL\n"
            "/start     start the bot\n"
            "/stop      stop the bot gracefully\n"
            "/status    bot process state + uptime\n"
            "\n"
            "⚠️  CAPTCHA\n"
            "/resume    unblock next stuck DM\n"
            "/queue     show full captcha queue\n"
            "\n"
            "📋  LOGS\n"
            "/logs on   enable log forwarding to Telegram\n"
            "/logs off  mute log forwarding\n"
            "/logs      check current log state\n"
            "\n"
            "❓  OTHER\n"
            "/help      show this message\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡  /resume and /queue only work when bot is running\n"
            "💡  captcha alerts always come through even with /logs off"
        ),
    )


# ── forward commands to main.py's internal handler ────────────────────────────
# When main.py runs in WATCHDOG_MODE, its telegram_bot.py has no poll loop
# but DOES have _handle_update() logic. We replicate the routing here
# so /resume, /queue, /logs are handled by importing from tg directly.
# Since watchdog and main.py are SEPARATE processes, we can't import tg.
# Instead we re-implement the minimal forwarding via the shared SQLite state
# and direct Telegram sends — /resume is the only stateful one.
#
# SOLUTION: watchdog sends the command to the Telegram bot API as if the
# user sent it again, but routed through main.py's OWN webhook/polling.
# Since main.py has NO poll loop in WATCHDOG_MODE, watchdog must handle
# /resume and /queue itself by reading the shared SQLite dm_queue table
# and the shared tg/state.py captcha_queue — but those live in main.py's
# process memory, not on disk.
#
# PRACTICAL FIX: watchdog handles /resume and /queue by reading dm_queue
# from SQLite (which IS on disk) for status, and signals main.py via a
# lightweight signal file that main.py polls every second.

_SIGNAL_DIR  = Path(__file__).resolve().parent / ".watchdog_signals"
_SIGNAL_DIR.mkdir(exist_ok=True)


def _write_signal(name: str) -> None:
    """Write a signal file that main.py picks up."""
    sig_path = _SIGNAL_DIR / name
    sig_path.write_text(str(time.time()))
    logger.info("Signal written: %s", name)


async def _cmd_resume(session: aiohttp.ClientSession) -> None:
    if not _is_running():
        await _send(session, "⚠️ Bot is not running — start it first with /start")
        return
    _write_signal("resume")
    await _send(session, "⏳ Resume signal sent to bot — check logs for confirmation")


async def _cmd_queue(session: aiohttp.ClientSession) -> None:
    if not _is_running():
        await _send(session, "⚠️ Bot is not running — captcha queue is empty")
        return
    _write_signal("queue_status")
    await _send(session, "📋 Queue status requested — bot will reply shortly")


async def _cmd_logs(session: aiohttp.ClientSession, text: str) -> None:
    if not _is_running():
        await _send(session, "⚠️ Bot is not running")
        return
    if "off" in text:
        _write_signal("logs_off")
        await _send(session, "🔇 Log mute signal sent to bot")
    elif "on" in text:
        _write_signal("logs_on")
        await _send(session, "🔊 Log enable signal sent to bot")
    else:
        _write_signal("logs_status")
        await _send(session, "📋 Log status requested — bot will reply shortly")


# ── main loop ──────────────────────────────────────────────────────────────────

async def run() -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ERROR: TG_BOT_TOKEN and TG_CHAT_ID must be set in .env", file=sys.stderr)
        sys.exit(1)

    logger.info("Watchdog started — sole Telegram poller on this token")

    async with aiohttp.ClientSession() as session:
        offset = await _drain_stale(session)

        await _send(
            session,
            (
                "👀  Watchdog online\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "/start  — start the bot\n"
                "/stop   — stop the bot\n"
                "/status — process state\n"
                "/help   — all commands"
            ),
        )

        while True:
            updates = await _get_updates(session, offset)
            if not updates:
                # Check for unexpected crash between polls
                _check_unexpected_crash(session)
                await asyncio.sleep(0)
                continue

            for update in updates:
                offset  = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                sender_id: int = message.get("chat", {}).get("id", -1)
                text:      str = (message.get("text") or "").strip().lower()

                if sender_id != TG_CHAT_ID:
                    continue

                # Check for crash before handling every command
                await _handle_crash_check(session)

                # Route
                if text.startswith("/start"):
                    await _cmd_start(session)
                elif text.startswith("/stop"):
                    await _cmd_stop(session)
                elif text.startswith("/status"):
                    await _cmd_status(session)
                elif text.startswith("/resume"):
                    await _cmd_resume(session)
                elif text.startswith("/queue"):
                    await _cmd_queue(session)
                elif text.startswith("/logs"):
                    await _cmd_logs(session, text)
                elif text.startswith("/help"):
                    await _cmd_help(session)
                else:
                    await _send(
                        session,
                        f"❓ Unknown command: {text}\nSend /help for the full list.",
                    )


# ── crash detection ────────────────────────────────────────────────────────────

_crash_notified: bool = False


def _check_unexpected_crash(session: aiohttp.ClientSession) -> None:
    """Sync check — only flags, actual notify is async in _handle_crash_check."""
    global _crash_notified
    if _proc is not None and not _is_running() and not _crash_notified:
        _crash_notified = True


async def _handle_crash_check(session: aiohttp.ClientSession) -> None:
    global _proc, _started_at, _crash_notified
    if _crash_notified:
        logger.warning("main.py exited unexpectedly")
        await _send(session, "⚠️ Bot crashed unexpectedly — send /start to restart")
        _proc        = None
        _started_at  = None
        _crash_notified = False


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Watchdog shutting down")
        if _is_running() and _proc is not None:
            logger.info("Terminating main.py  (pid: %d)", _proc.pid)
            _proc.terminate()


if __name__ == "__main__":
    main()