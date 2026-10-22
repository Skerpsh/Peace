"""Опрос официального API тревог — «эталон» для статуса тревоги (есть / нет).

Используется alerts.in.ua (токен по запросу). Документация: https://devs.alerts.in.ua/
UID регионов (проверить по документации): м. Київ = 31, Київська область = 14.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from peace.config import settings
from peace.models import AlertLevel, ParsedEvent

log = logging.getLogger(__name__)

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
REGION_MAP = {"31": "kyiv_all", "14": "oblast_all"}
OFFICIAL_SOURCE = "official_api"
POLL_SECONDS = 15


async def run_official_poller(queue: asyncio.Queue[ParsedEvent]) -> None:
    token = settings().alerts_api_token
    if not token:
        log.warning("ALERTS_API_TOKEN не задан — официальный API отключён")
        return

    headers = {"Authorization": f"Bearer {token}"}
    prev_active: set[str] = set()

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    r.raise_for_status()
                    data = await r.json()
                active = {
                    REGION_MAP[str(a.get("location_uid"))]
                    for a in data.get("alerts", [])
                    if str(a.get("location_uid")) in REGION_MAP
                }
                now = datetime.now(timezone.utc)
                # Шлём событие только при изменении состояния
                for district in active - prev_active:
                    await queue.put(_event(district, now, all_clear=False))
                for district in prev_active - active:
                    await queue.put(_event(district, now, all_clear=True))
                prev_active = active
            except Exception as e:  # noqa: BLE001
                log.warning("Official API error: %s", e)
            await asyncio.sleep(POLL_SECONDS)


def _event(district: str, ts: datetime, all_clear: bool) -> ParsedEvent:
    return ParsedEvent(
        source=OFFICIAL_SOURCE,
        message_id=0,
        timestamp=ts,
        districts=[district],
        # API знает только факт тревоги, но не тип угрозы -> цвет уточнят каналы
        level=AlertLevel.NONE if all_clear else AlertLevel.ALERT,
        threats=[],
        direction=None,
        is_all_clear=all_clear,
        raw_text="official api",
    )
