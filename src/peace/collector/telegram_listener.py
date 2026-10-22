"""Reads Telegram channels using a user account (Telethon / MTProto).

Combines three mechanisms so no channel message is ever missed:
1. Joins target channels via JoinChannelRequest so MTProto real-time push works.
2. Preloads recent messages (last 2 hours) on startup to populate initial /status state.
3. Runs a 10-second get_messages(min_id=...) polling loop alongside NewMessage/MessageEdited
   push handlers (large broadcast channels in Telegram often delay passive push updates).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest

from peace.config import channels, settings
from peace.models import RawMessage

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10
PRELOAD_LOOKBACK = timedelta(hours=2)
PRELOAD_LIMIT = 15


async def run_listener(queue: asyncio.Queue[RawMessage]) -> None:
    cfg = settings()
    raw_usernames = [
        c["username"].lstrip("@")
        for c in channels()
        if c.get("username") and not str(c["username"]).upper().startswith("CHANGE_ME")
    ]

    Path(cfg.tg_session_name).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(cfg.tg_session_name, cfg.tg_api_id, cfg.tg_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Userbot is not authorized. Run once: PYTHONPATH=src .venv/bin/python -m peace.login"
        )

    valid_chats = []
    valid_names: list[str] = []
    for uname in raw_usernames:
        try:
            entity = await client.get_entity(uname)
            valid_chats.append(entity)
            valid_names.append(getattr(entity, "username", None) or uname)
            try:
                await client(JoinChannelRequest(entity))
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            log.warning("Skipping unresolved channel '%s': %s", uname, e)

    if not valid_chats:
        log.warning("No valid channels resolved from config/channels.yaml; listener is idle")
        await client.run_until_disconnected()
        return

    seen: set[tuple[str, int, str]] = set()
    last_id: dict[str, int] = {}

    async def _emit(source: str, msg_id: int, text: str, date: datetime, edited: bool, preload: bool) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        key = (source.lower(), msg_id, clean)
        if key in seen:
            return
        seen.add(key)
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

    # 1. Preload recent messages (last 2 hours) sorted chronologically so /status is ready immediately
    cutoff = datetime.now(timezone.utc) - PRELOAD_LOOKBACK
    preload_batch: list[tuple[str, int, str, datetime]] = []
    for entity, uname in zip(valid_chats, valid_names):
        try:
            msgs = await client.get_messages(entity, limit=PRELOAD_LIMIT)
            for m in msgs:
                if not m or not getattr(m, "id", None):
                    continue
                if m.id > last_id.get(uname.lower(), 0):
                    last_id[uname.lower()] = m.id
                if m.date and m.date >= cutoff and (m.raw_text or "").strip():
                    preload_batch.append((uname, m.id, m.raw_text, m.date))
        except Exception as e:  # noqa: BLE001
            log.warning("Preload failed for '%s': %s", uname, e)

    for uname, mid, text, dt in sorted(preload_batch, key=lambda x: x[3]):
        await _emit(uname, mid, text, dt, edited=False, preload=True)
    log.info("Preloaded %d recent messages from %d channels", len(preload_batch), len(valid_names))

    # 2. Real-time push handlers
    async def _push_event(event, edited: bool) -> None:
        chat = await event.get_chat()
        source = getattr(chat, "username", None) or str(event.chat_id)
        await _emit(source, event.id, event.raw_text or "", event.date, edited=edited, preload=False)

    @client.on(events.NewMessage(chats=valid_chats))
    async def on_new(event):
        await _push_event(event, edited=False)

    @client.on(events.MessageEdited(chats=valid_chats))
    async def on_edit(event):
        await _push_event(event, edited=True)

    # 3. Active polling loop (catches messages in large broadcast channels where MTProto push is delayed)
    async def _poll_loop() -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            for entity, uname in zip(valid_chats, valid_names):
                try:
                    min_id = last_id.get(uname.lower(), 0)
                    msgs = await client.get_messages(entity, limit=5, min_id=min_id)
                    for m in sorted(msgs, key=lambda x: x.id):
                        if m and getattr(m, "id", None) and (m.raw_text or "").strip():
                            await _emit(uname, m.id, m.raw_text, m.date, edited=False, preload=False)
                except Exception as e:  # noqa: BLE001
                    log.debug("Poll error for '%s': %s", uname, e)

    log.info("Listening & polling %d channels: %s", len(valid_names), valid_names)
    await asyncio.gather(client.run_until_disconnected(), _poll_loop())
