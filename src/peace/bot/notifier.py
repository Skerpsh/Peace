"""Delivers situation changes: short pushes for important changes plus one live status message per chat.

Push policy: a push is sent when a zone's alert level changes or a new threat type appears.
Smaller changes (confirmation, direction, sources) only edit the live "status now" message.
In night mode only red escalations and all-clears are pushed.

Speed: escalations are delivered at once (not batched), pushes go out in parallel at Telegram's
rate limit with red ones first, and live statuses are edited only after every push is out.
An unconfirmed non-red alert arrives without sound; red alerts and all-clears always ring.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import KYIV_TZ, RANK, format_live, format_update, with_start
from peace.config import settings
from peace.models import AlertLevel, Situation
from peace.storage.db import Database, Recipient

log = logging.getLogger(__name__)

SEND_PER_SEC = 25  # Telegram allows bots about 30 messages per second in total


def is_night(now: datetime | None = None) -> bool:
    cfg = settings()
    h = (now or datetime.now(timezone.utc)).astimezone(KYIV_TZ).hour
    if cfg.quiet_start_hour > cfg.quiet_end_hour:   # window crosses midnight
        return h >= cfg.quiet_start_hour or h < cfg.quiet_end_hour
    return cfg.quiet_start_hour <= h < cfg.quiet_end_hour


def should_push(old: Situation | None, new: Situation, quiet: bool = False) -> bool:
    old_level = old.level if old else AlertLevel.NONE
    new_threats = set(new.threats) - set(old.threats if old else [])
    if quiet:
        if new.level is AlertLevel.NONE:
            return old_level is not AlertLevel.NONE   # all-clear
        return new.level is AlertLevel.RED and (old_level is not AlertLevel.RED or bool(new_threats))
    return old_level != new.level or bool(new_threats)


def is_escalation(old: Situation | None, new: Situation) -> bool:
    """A worse situation: a higher level or a new threat type. Delivered without batching."""
    if new.level is AlertLevel.NONE:
        return False
    old_level = old.level if old else AlertLevel.NONE
    return RANK[new.level] > RANK[old_level] or bool(set(new.threats) - set(old.threats if old else []))


def is_silent(push: list[Situation]) -> bool:
    """Only unconfirmed yellow/untyped alerts arrive without sound."""
    return all(not s.confirmed and s.level in (AlertLevel.YELLOW, AlertLevel.ALERT) for s in push)


class Notifier:
    def __init__(self, bot: Bot, db: Database, agg: Aggregator) -> None:
        self.bot = bot
        self.db = db
        self.agg = agg
        self.last_sent: dict[str, Situation] = {}
        self.ready = False   # no deliveries until the startup state is synced
        self.counters: Counter[str] = Counter()   # push / failed / blocked, read by the monitor
        # (time, seconds from the source post to the last push of it), read by the monitor
        self.delays: deque[tuple[float, float]] = deque(maxlen=2000)

    def is_urgent(self, s: Situation) -> bool:
        return self.ready and is_escalation(self.last_sent.get(s.district), s)

    def sync(self) -> None:
        """Marks the current aggregated state as already delivered (used after startup preload)."""
        self.last_sent = {s.district: s for s in self.agg.snapshot()}
        self.ready = True

    async def flush(self, changed: dict[str, Situation]) -> None:
        """Delivers a batch of changed zones; flip-flops that end where they started are dropped."""
        if not self.ready:
            return
        old = {d: self.last_sent.get(d) for d in changed}
        deltas = {d: with_start(s, old[d]) for d, s in changed.items()
                  if Aggregator.is_meaningful_change(old[d], s)}
        if not deltas:
            return
        self.last_sent.update(deltas)
        active = self.agg.snapshot()
        night = is_night()

        recipients = [r for r in await self.db.recipients() if any(r.wants(d) for d in deltas)]
        jobs = []
        for r in recipients:
            push = [s for d, s in deltas.items()
                    if r.wants(d) and should_push(old[d], s, quiet=r.quiet and night)]
            if push:
                jobs.append((r.chat_id, push))
        jobs.sort(key=lambda j: -max(RANK[s.level] for s in j[1]))   # red first
        log.info("Delivering %d changed zone(s): %d push(es), %d live status(es)",
                 len(deltas), len(jobs), len(recipients))

        await self._paced([lambda c=c, p=p: self._push(c, p) for c, p in jobs])
        if jobs:
            now = datetime.now(timezone.utc)
            pushed = {s.district for _, p in jobs for s in p}
            for d in pushed:
                if deltas[d].updated_at:
                    self.delays.append((time.time(), (now - deltas[d].updated_at).total_seconds()))
        await self._paced([lambda r=r: self.refresh_live(r, active) for r in recipients])

    async def _push(self, chat_id: int, push: list[Situation]) -> None:
        try:
            if await self._safe(chat_id, lambda: self.bot.send_message(
                    chat_id, format_update(push), disable_web_page_preview=True,
                    disable_notification=is_silent(push))):
                self.counters["push"] += 1
        except Exception as e:  # noqa: BLE001
            self.counters["failed"] += 1
            log.warning("Push to %s failed: %s", chat_id, e)

    @staticmethod
    async def _paced(calls: list[Callable[[], Awaitable[None]]]) -> None:
        """Runs calls in parallel, at most SEND_PER_SEC per second."""
        loop = asyncio.get_running_loop()
        for i in range(0, len(calls), SEND_PER_SEC):
            started = loop.time()
            await asyncio.gather(*(c() for c in calls[i:i + SEND_PER_SEC]))
            if i + SEND_PER_SEC < len(calls):
                await asyncio.sleep(max(0.0, 1.0 - (loop.time() - started)))

    async def refresh_all(self) -> None:
        """Re-renders every live status (keeps 'outdated' notices honest during long alerts)."""
        active = self.agg.snapshot()
        await self._paced([lambda r=r: self.refresh_live(r, active) for r in await self.db.recipients()])

    async def refresh_live(self, r: Recipient, active: list[Situation]) -> None:
        text = format_live([s for s in active if r.wants(s.district)])
        if r.status_msg_id:
            try:
                await self._safe(r.chat_id, lambda: self.bot.edit_message_text(
                    text, chat_id=r.chat_id, message_id=r.status_msg_id, disable_web_page_preview=True))
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
                log.info("Live status of %s lost (%s), sending a new one", r.chat_id, e)
            except Exception as e:  # noqa: BLE001
                log.warning("Live status edit for %s failed: %s", r.chat_id, e)
                return
        await self.send_live(r.chat_id, r.zones)

    async def send_live(self, chat_id: int, zones: set[str]) -> None:
        """Sends a fresh live status message, pins it, and remembers it for future edits."""
        active = [s for s in self.agg.snapshot()
                  if not zones or "*" in zones or s.district in zones]
        try:
            msg = await self._safe(chat_id, lambda: self.bot.send_message(
                chat_id, format_live(active), disable_web_page_preview=True))
        except Exception as e:  # noqa: BLE001
            log.warning("Live status send to %s failed: %s", chat_id, e)
            return
        if not msg:
            return
        await self.db.set_status_msg(chat_id, msg.message_id)
        try:
            await self.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except Exception as e:  # noqa: BLE001
            log.debug("Pin in %s failed: %s", chat_id, e)

    async def _safe(self, chat_id: int, call):
        """Runs a Bot API call with flood-wait retry; forgets chats that blocked the bot.

        Returns None when the chat is gone or retries ran out; other errors propagate.
        """
        for _ in range(3):
            try:
                return await call()
            except TelegramRetryAfter as e:
                log.warning("Flood control: waiting %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                log.info("Chat %s blocked the bot, removing it", chat_id)
                self.counters["blocked"] += 1
                await self.db.forget(chat_id)
                return None
        return None
