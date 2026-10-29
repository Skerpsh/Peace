"""Reads Telegram channels in real time.

Primary collector: polls public channel web previews (https://t.me/s/<channel>) every 5 seconds
via aiohttp — works immediately with zero Telethon login or userbot session required.
Optional secondary collector: if a valid Telethon userbot session exists on disk, also attaches
MTProto push handlers in the background.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html
import logging
from pathlib import Path
import re

import aiohttp

from peace.config import channels, settings
from peace.health import health
from peace.models import RawMessage

log = logging.getLogger(__name__)

WEB_POLL_INTERVAL_SEC = 5
PRELOAD_LOOKBACK = timedelta(minutes=90)
MAX_SEEN = 5000

_POST_RE = re.compile(r'data-post="([^"]+)/(\d+)"(.*?)(?=data-post="|\Z)', re.S)
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_web_channel_html(body: str, fallback_username: str) -> list[tuple[str, int, str, datetime]]:
    """Extracts (username, message_id, text, timestamp) from https://t.me/s/<channel> HTML."""
    results: list[tuple[str, int, str, datetime]] = []
    for m in _POST_RE.finditer(body):
        ch_name, mid_str, chunk = m.group(1), m.group(2), m.group(3)
        t_match = _TEXT_RE.search(chunk)
        dt_match = _TIME_RE.search(chunk)
        if not t_match or not dt_match:
            continue
        txt = html.unescape(_BR_RE.sub("\n", t_match.group(1)))
        txt = _TAG_RE.sub(" ", txt).strip()
        if not txt:
            continue
        try:
            dt = datetime.fromisoformat(dt_match.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            results.append((ch_name or fallback_username, int(mid_str), txt, dt))
        except ValueError:
            continue
    return results


async def _fetch_channel_web(
    session: aiohttp.ClientSession, username: str
) -> list[tuple[str, int, str, datetime]]:
    url = f"https://t.me/s/{username}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        body = await resp.text()
    return parse_web_channel_html(body, username)


async def _poll_all(
    session: aiohttp.ClientSession, usernames: list[str]
) -> list[tuple[str, list[tuple[str, int, str, datetime]]]]:
    """Fetches all channels in parallel; failures are reported to health and skipped."""
    results = await asyncio.gather(
        *(_fetch_channel_web(session, u) for u in usernames), return_exceptions=True
    )
    ok = []
    for uname, posts in zip(usernames, results):
        if isinstance(posts, BaseException):
            if uname not in health.failing_since:
                log.warning("Web fetch failed for '%s': %r", uname, posts)
            health.fail(uname, repr(posts))
            continue
        health.ok(uname)
        ok.append((uname, posts))
    return ok


async def run_listener(
    queue: asyncio.Queue[RawMessage],
    preload_done: asyncio.Event | None = None,
) -> None:
    raw_usernames = [
        c["username"].lstrip("@")
        for c in channels()
        if c.get("username") and not str(c["username"]).upper().startswith("CHANGE_ME")
    ]
    if not raw_usernames:
        log.warning("No channels configured in config/channels.yaml")
        if preload_done:
            preload_done.set()
        return

    seen: dict[tuple[str, int, str], None] = {}  # insertion-ordered set
    texts: dict[tuple[str, int], str] = {}  # latest text per post, to recognise edits
    last_id: dict[str, int] = {}

    async def _emit(
        source: str, msg_id: int, text: str, date: datetime, edited: bool, preload: bool
    ) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        key = (source.lower(), msg_id, clean)
        if key in seen:
            return
        # The same post with new text is an edit, whichever collector saw it; the web page and
        # Telethon space the same post differently, so spacing alone is not an edit
        flat = "".join(clean.split())
        edited = edited or texts.get(key[:2], flat) != flat
        seen[key] = None
        texts[key[:2]] = flat
        if len(seen) > MAX_SEEN:
            for old in list(seen)[: MAX_SEEN // 5]:
                del seen[old]
                texts.pop(old[:2], None)
        if msg_id > last_id.get(source.lower(), 0):
            last_id[source.lower()] = msg_id
        await queue.put(
            RawMessage(
                source=source,
                message_id=msg_id,
                text=clean,
                date=date,
                edited=edited,
                preload=preload,
            )
        )

    async def _delete(source: str, msg_id: int) -> None:
        await queue.put(RawMessage(source=source, message_id=msg_id, text="",
                                   date=datetime.now(timezone.utc), deleted=True))

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. Initial preload from the last 90 minutes across all channels
        cutoff = datetime.now(timezone.utc) - PRELOAD_LOOKBACK
        preload_batch: list[tuple[str, int, str, datetime]] = []
        for uname, posts in await _poll_all(session, raw_usernames):
            for src, mid, txt, dt in posts:
                if mid > last_id.get(uname.lower(), 0):
                    last_id[uname.lower()] = mid
                if dt >= cutoff:
                    preload_batch.append((uname, mid, txt, dt))

        for uname, mid, txt, dt in sorted(preload_batch, key=lambda x: x[3]):
            await _emit(uname, mid, txt, dt, edited=False, preload=True)

        log.info(
            "Preloaded %d recent messages (last 90m) from %d channels: %s",
            len(preload_batch),
            len(raw_usernames),
            raw_usernames,
        )
        if preload_done:
            preload_done.set()

        # 2. Optional Telethon listener if session file exists
        telethon_task = asyncio.create_task(_optional_telethon_listener(raw_usernames, _emit, _delete))

        # 3. Continuous 5-second web poller
        try:
            while True:
                await asyncio.sleep(WEB_POLL_INTERVAL_SEC)
                for uname, posts in await _poll_all(session, raw_usernames):
                    min_seen = last_id.get(uname.lower(), 0)
                    for _, mid, txt, dt in sorted(posts, key=lambda x: x[1]):
                        # Accept new messages or edits to the last 3 messages
                        if mid > min_seen or mid >= max(1, min_seen - 2):
                            await _emit(uname, mid, txt, dt, edited=mid <= min_seen, preload=False)
        finally:
            telethon_task.cancel()


async def _optional_telethon_listener(raw_usernames: list[str], emit_fn, delete_fn) -> None:
    """Attaches Telethon push handlers if an authorized session file exists; never crashes the bot."""
    cfg = settings()
    session_file = Path(f"{cfg.tg_session_name}.session")
    if not cfg.tg_api_id or not cfg.tg_api_hash or not session_file.exists():
        log.info("Running in web-poller mode (no Telethon session needed)")
        return

    try:
        from telethon import TelegramClient, events
        from telethon.tl.functions.channels import JoinChannelRequest

        client = TelegramClient(cfg.tg_session_name, cfg.tg_api_id, cfg.tg_api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            log.info("Telethon session not authorized; continuing with web-poller mode")
            await client.disconnect()
            return

        from telethon.utils import get_peer_id

        valid_chats = []
        # Channels with collectible usernames have chat.username = None; keep the configured name
        # so sources match channels.yaml and the web poller (weights, default district, dedup).
        names: dict[int, str] = {}
        for uname in raw_usernames:
            try:
                entity = await client.get_entity(uname)
                valid_chats.append(entity)
                names[get_peer_id(entity)] = uname
                try:
                    await client(JoinChannelRequest(entity))
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

        if not valid_chats:
            await client.disconnect()
            return

        @client.on(events.NewMessage(chats=valid_chats))
        async def on_new(event):
            source = names.get(event.chat_id, str(event.chat_id))
            await emit_fn(source, event.id, event.raw_text or "", event.date, False, False)

        @client.on(events.MessageEdited(chats=valid_chats))
        async def on_edit(event):
            source = names.get(event.chat_id, str(event.chat_id))
            await emit_fn(source, event.id, event.raw_text or "", event.date, True, False)

        @client.on(events.MessageDeleted(chats=valid_chats))
        async def on_delete(event):
            source = names.get(event.chat_id, str(event.chat_id))
            for msg_id in event.deleted_ids:
                await delete_fn(source, msg_id)

        log.info("Telethon push listener attached alongside web poller")
        await client.run_until_disconnected()
    except Exception as e:  # noqa: BLE001
        log.warning("Telethon optional listener stopped (%s); web poller remains active", e)
