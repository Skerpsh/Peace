"""Reads Telegram channels using a user account (Telethon / MTProto).

Standard Bot API cannot read third-party channels, so a userbot is required.
The account must have joined or be able to view all channels in channels.yaml.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient, events

from peace.config import channels, settings
from peace.models import RawMessage

log = logging.getLogger(__name__)


async def run_listener(queue: asyncio.Queue[RawMessage]) -> None:
    cfg = settings()
    usernames = [c["username"] for c in channels()]

    Path(cfg.tg_session_name).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(cfg.tg_session_name, cfg.tg_api_id, cfg.tg_api_hash)
    # Do not call client.start() here: under systemd there is no interactive stdin for phone/code.
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Userbot is not authorized. Run once: PYTHONPATH=src .venv/bin/python -m peace.login"
        )

    async def _push(event, edited: bool) -> None:
        chat = await event.get_chat()
        msg = RawMessage(
            source=getattr(chat, "username", None) or str(event.chat_id),
            message_id=event.id,
            text=event.raw_text or "",
            date=event.date,
            edited=edited,
        )
        if msg.text.strip():
            await queue.put(msg)

    @client.on(events.NewMessage(chats=usernames))
    async def on_new(event):
        await _push(event, edited=False)

    # Channels frequently edit posts ("UPD: target intercepted", etc.)
    @client.on(events.MessageEdited(chats=usernames))
    async def on_edit(event):
        await _push(event, edited=True)

    log.info("Listening to %d channels: %s", len(usernames), usernames)
    await client.run_until_disconnected()
