"""Точка входа: запускает сбор, анализ и бота в одном event loop."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import format_situation
from peace.bot.handlers import build_router
from peace.collector.official_api import run_official_poller
from peace.collector.telegram_listener import run_listener
from peace.config import settings
from peace.models import ParsedEvent, RawMessage
from peace.parser.extractor import extract
from peace.storage.db import Database

log = logging.getLogger("peace")


async def pipeline(
    raw_q: asyncio.Queue[RawMessage],
    event_q: asyncio.Queue[ParsedEvent],
) -> None:
    """RawMessage -> ParsedEvent."""
    while True:
        raw = await raw_q.get()
        ev = extract(raw)
        if ev:
            await event_q.put(ev)


async def analyzer_loop(
    event_q: asyncio.Queue[ParsedEvent], agg: Aggregator, db: Database, bot: Bot
) -> None:
    """ParsedEvent -> Aggregator -> рассылка изменений подписчикам."""
    while True:
        ev = await event_q.get()
        await db.log_event(ev)
        for situation in agg.add(ev):
            text = format_situation(situation)
            for chat_id in await db.subscribers_for(situation.district):
                try:
                    await bot.send_message(chat_id, text)
                except Exception as e:  # noqa: BLE001
                    log.warning("send to %s failed: %s", chat_id, e)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = settings()

    db = Database(cfg.db_path)
    await db.connect()
    agg = Aggregator()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_router(db, agg))

    raw_q: asyncio.Queue[RawMessage] = asyncio.Queue()
    event_q: asyncio.Queue[ParsedEvent] = asyncio.Queue()

    await asyncio.gather(
        run_listener(raw_q),
        run_official_poller(event_q),
        pipeline(raw_q, event_q),
        analyzer_loop(event_q, agg, db, bot),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
