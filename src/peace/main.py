"""Application entry point: runs collectors, parser pipeline, aggregator, and bot in one event loop."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import format_situation, format_snapshot
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
            ev.preload = raw.preload
            log.info(
                "Matched [%s#%s]%s -> districts=%s level=%s threats=%s cleared=%s all_clear=%s",
                ev.source,
                ev.message_id,
                " (preload)" if ev.preload else "",
                ev.districts,
                ev.level.value,
                ev.threats,
                ev.cleared_threats,
                ev.is_all_clear,
            )
            await event_q.put(ev)
        else:
            preview = raw.text.replace("\n", " ")[:90]
            log.debug("Ignored [%s#%s]: %s", raw.source, raw.message_id, preview)


async def analyzer_loop(
    event_q: asyncio.Queue[ParsedEvent], agg: Aggregator, db: Database, bot: Bot
) -> None:
    """ParsedEvent -> Aggregator -> broadcast state changes to subscribers."""
    while True:
        ev = await event_q.get()
        await db.log_event(ev)
        changes = agg.add(ev)
        if ev.preload:
            continue
        for situation in changes:
            text = format_situation(situation)
            subs = await db.subscribers_for(situation.district)
            log.info(
                "Broadcasting update for %s (%s) to %d subscribers",
                situation.district,
                situation.level.value,
                len(subs),
            )
            for chat_id in subs:
                try:
                    await bot.send_message(chat_id, text)
                except Exception as e:  # noqa: BLE001
                    log.warning("send to %s failed: %s", chat_id, e)


async def _notify_startup_snapshot(
    preload_done: asyncio.Event,
    raw_q: asyncio.Queue[RawMessage],
    event_q: asyncio.Queue[ParsedEvent],
    agg: Aggregator,
    db: Database,
    bot: Bot,
) -> None:
    """Once startup preload finishes, send the current active snapshot to existing subscribers."""
    await preload_done.wait()
    # Allow queues to drain preloaded items
    while not raw_q.empty() or not event_q.empty():
        await asyncio.sleep(0.1)

    subs = await db.subscribers_for("*")
    active = agg.snapshot()
    log.info("Startup ready: %d active district(s), %d subscriber(s) in DB", len(active), len(subs))
    if not subs:
        log.warning("No subscribers in DB yet! Open your bot in Telegram and send /start")
        return
    if active:
        text = format_snapshot(active)
        for chat_id in subs:
            try:
                await bot.send_message(chat_id, text)
            except Exception as e:  # noqa: BLE001
                log.warning("startup send to %s failed: %s", chat_id, e)


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
    preload_done = asyncio.Event()

    await asyncio.gather(
        run_listener(raw_q, preload_done),
        run_official_poller(event_q),
        pipeline(raw_q, event_q),
        analyzer_loop(event_q, agg, db, bot),
        _notify_startup_snapshot(preload_done, raw_q, event_q, agg, db, bot),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
