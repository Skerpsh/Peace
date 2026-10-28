"""Application entry point: runs collectors, parser pipeline, aggregator, and bot in one event loop."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from peace.analyzer.aggregator import Aggregator
from peace.bot.handlers import COMMANDS, build_router
from peace.bot.notifier import Notifier
from peace.collector.official_api import run_official_poller
from peace.collector.telegram_listener import run_listener
from peace.config import settings
from peace.health import health
from peace.models import ParsedEvent, RawMessage, Situation
from peace.monitor import Monitor
from peace.parser.extractor import extract
from peace.storage.db import Database

log = logging.getLogger("peace")

LIVE_REFRESH_SEC = 5 * 60


async def pipeline(
    raw_q: asyncio.Queue[RawMessage],
    event_q: asyncio.Queue[ParsedEvent],
    db: Database,
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
            if not raw.preload:
                await db.log_ignored(raw)


async def analyzer_loop(
    event_q: asyncio.Queue[ParsedEvent], agg: Aggregator, db: Database, notifier: Notifier
) -> None:
    """ParsedEvent -> Aggregator -> batched delivery.

    Changes are collected for BATCH_SECONDS after the first one, so a burst of posts about the
    same attack becomes one notification and other channels get time to confirm it.
    """
    loop = asyncio.get_running_loop()
    pending: dict[str, Situation] = {}
    deadline: float | None = None
    while True:
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        try:
            ev = await asyncio.wait_for(event_q.get(), timeout)
        except asyncio.TimeoutError:
            batch, pending, deadline = pending, {}, None
            try:
                await notifier.flush(batch)
            except Exception:  # noqa: BLE001
                log.exception("Delivery failed")
            continue
        await db.log_event(ev)
        changes = agg.add(ev)
        if ev.preload:
            continue
        for situation in changes:
            pending[situation.district] = situation
        if pending and deadline is None:
            deadline = loop.time() + settings().batch_seconds


async def _startup(
    preload_done: asyncio.Event,
    raw_q: asyncio.Queue[RawMessage],
    event_q: asyncio.Queue[ParsedEvent],
    agg: Aggregator,
    db: Database,
    notifier: Notifier,
) -> None:
    """Once startup preload finishes, mark it delivered and refresh everyone's live status (no pushes)."""
    await preload_done.wait()
    # Allow queues to drain preloaded items
    while not raw_q.empty() or not event_q.empty():
        await asyncio.sleep(0.1)

    notifier.sync()
    recipients = await db.recipients()
    active = agg.snapshot()
    log.info("Startup ready: %d active zone(s), %d subscriber(s) in DB", len(active), len(recipients))
    if not recipients:
        log.warning("No subscribers in DB yet! Open your bot in Telegram and send /start")
    await notifier.refresh_all()

    while True:
        await asyncio.sleep(LIVE_REFRESH_SEC)
        if agg.snapshot():
            await notifier.refresh_all()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = settings()

    db = Database(cfg.db_path)
    await db.connect()
    agg = Aggregator()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    notifier = Notifier(bot, db, agg)
    monitor = Monitor(bot, db, agg, notifier, health)
    dp = Dispatcher()
    dp.include_router(build_router(db, notifier, monitor))
    await bot.set_my_commands(COMMANDS)

    raw_q: asyncio.Queue[RawMessage] = asyncio.Queue()
    event_q: asyncio.Queue[ParsedEvent] = asyncio.Queue()
    preload_done = asyncio.Event()

    await asyncio.gather(
        run_listener(raw_q, preload_done),
        run_official_poller(event_q),
        pipeline(raw_q, event_q, db),
        analyzer_loop(event_q, agg, db, notifier),
        _startup(preload_done, raw_q, event_q, agg, db, notifier),
        health.watch(monitor.send),
        monitor.run(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
