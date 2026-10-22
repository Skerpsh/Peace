"""Polls the official air alerts API as the ground-truth source for alert active/all-clear status.

Uses alerts.in.ua (API token available upon request). Docs: https://devs.alerts.in.ua/
Region UIDs (verify against official docs): Kyiv City = 31, Kyiv Oblast = 14.
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
        log.warning("ALERTS_API_TOKEN is not set — official alerts API poller is disabled")
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
                # Emit an event only when the active state changes
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
        # Official API only reports whether an alert is active, not the specific threat type
        level=AlertLevel.NONE if all_clear else AlertLevel.ALERT,
        threats=[],
        direction=None,
        is_all_clear=all_clear,
        raw_text="official api",
    )
