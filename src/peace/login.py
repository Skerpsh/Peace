"""Одноразовый вход userbot-аккаунта: спрашивает телефон, код (и пароль 2FA) и создаёт файл сессии.

Запуск (на VPS или локально):
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
    await client.start()  # интерактивно: телефон -> код -> пароль 2FA
    me = await client.get_me()
    print(f"OK: вошли как {me.first_name} (@{me.username}). Сессия: {cfg.tg_session_name}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
