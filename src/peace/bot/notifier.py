"""Delivers situation changes: short pushes for important changes plus one live status message per chat.

Push policy: a push is sent when a zone's alert level changes or a new threat type appears.
Smaller changes (confirmation, direction, sources) only edit the live "status now" message.
In night mode only red escalations and all-clears are pushed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import KYIV_TZ, RANK, format_live, format_update
from peace.config import settings
from peace.models import AlertLevel, Situation
from peace.storage.db import Database, Recipient

log = logging.getLogger(__name__)

SEND_GAP_SEC = 0.05  # stay well under Telegram's ~30 messages/second bot limit


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


class Notifier:
    def __init__(self, bot: Bot, db: Database, agg: Aggregator) -> None:
        self.bot = bot
        self.db = db
        self.agg = agg
        self.last_sent: dict[str, Situation] = {}
        self.ready = False   # no deliveries until the startup state is synced

    def sync(self) -> None:
        """Marks the current aggregated state as already delivered (used after startup preload)."""
        self.last_sent = {s.district: s for s in self.agg.snapshot()}
        self.ready = True

    async def flush(self, changed: dict[str, Situation]) -> None:
        """Delivers a batch of changed zones; flip-flops that end where they started are dropped."""
        if not self.ready:
            return
        old = {d: self.last_sent.get(d) for d in changed}
        deltas = {d: s for d, s in changed.items() if Aggregator.is_meaningful_change(old[d], s)}
        if not deltas:
            return
        self.last_sent.update(deltas)
        active = self.agg.snapshot()
        night = is_night()

        recipients = [r for r in await self.db.recipients() if any(r.wants(d) for d in deltas)]
        log.info("Delivering %d changed zone(s) to %d chat(s)", len(deltas), len(recipients))
        for r in recipients:
            push = [s for d, s in deltas.items()
                    if r.wants(d) and should_push(old[d], s, quiet=r.quiet and night)]
            if push:
                try:
                    await self._safe(r.chat_id, lambda r=r, push=push: self.bot.send_message(
                        r.chat_id, format_update(push), disable_web_page_preview=True))
                except Exception as e:  # noqa: BLE001
                    log.warning("Push to %s failed: %s", r.chat_id, e)
            await self.refresh_live(r, active)

    async def refresh_all(self) -> None:
        """Re-renders every live status (keeps 'outdated' notices honest during long alerts)."""
        active = self.agg.snapshot()
        for r in await self.db.recipients():
            await self.refresh_live(r, active)

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
                result = await call()
                await asyncio.sleep(SEND_GAP_SEC)
                return result
            except TelegramRetryAfter as e:
                log.warning("Flood control: waiting %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                log.info("Chat %s blocked the bot, removing it", chat_id)
                await self.db.forget(chat_id)
                return None
        return None
