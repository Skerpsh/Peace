"""One-time interactive login for the Telethon userbot account.

Prompts for phone number, login code (and optional 2FA password) and saves the session file.

Usage:
    PYTHONPATH=src .venv/bin/python -m peace.login
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient

from peace.config import settings


async def main() -> None:
    cfg = settings()
    Path(cfg.tg_session_name).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(cfg.tg_session_name, cfg.tg_api_id, cfg.tg_api_hash)
    await client.start()  # Interactive: phone -> code -> 2FA password
    me = await client.get_me()
    print(f"OK: logged in as {me.first_name} (@{me.username}). Session: {cfg.tg_session_name}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
