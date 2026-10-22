"""Polls the official alerts.in.ua API (/v1/alerts/active.json).

Supports:
- UID 31 (Kyiv City) and UID 14 (Kyiv Oblast + its raions via location_oblast_uid / location_raion)
- Alert levels ('red', 'yellow') and structured 'threats' array from alerts.in.ua
- Partial threat clearance when a threat disappears from an active alert
- HTTP caching via If-Modified-Since / Last-Modified (304 Not Modified)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from peace.config import regions, settings
from peace.models import AlertLevel, ParsedEvent
from peace.parser.extractor import level_for_threats
from peace.parser.normalizer import normalize

log = logging.getLogger(__name__)

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
OFFICIAL_SOURCE = "official_api"
POLL_SECONDS = 10  # 6 req/min, safely below the 8-10 req/min soft limit

THREAT_TYPE_MAP = {
    "ballistic_missiles": "ballistic",
    "cruise_missiles": "cruise_missile",
    "unspecified_missiles": "missile",
    "mig31k_departure": "aviation",
    "strategic_aircraft_activity": "aviation",
    "tactic_aircraft_activity": "aviation",
    "drones": "shahed",
}

LEVEL_MAP = {
    "red": AlertLevel.RED,
    "yellow": AlertLevel.YELLOW,
}


def _match_district(alert: dict) -> str | None:
    uid = str(alert.get("location_uid") or "")
    oblast_uid = str(alert.get("location_oblast_uid") or "")
    title = normalize(
        f"{alert.get('location_title', '')} {alert.get('location_raion', '')}"
    )

    if uid == "31" or oblast_uid == "31" or alert.get("location_title") == "м. Київ":
        for key, info in regions().items():
            if info["area"] == "kyiv_city" and key != "kyiv_all":
                if any(alias in title for alias in info["aliases"]):
                    return key
        return "kyiv_all"

    if uid == "14" or oblast_uid == "14" or alert.get("location_oblast") == "Київська область":
        for key, info in regions().items():
            if info["area"] == "kyiv_oblast" and key != "oblast_all":
                if any(alias in title for alias in info["aliases"]):
                    return key
        return "oblast_all"

    return None


def parse_active_alerts(
    payload: dict,
    prev_state: dict[str, tuple[AlertLevel, frozenset[str]]],
    now: datetime | None = None,
) -> tuple[list[ParsedEvent], dict[str, tuple[AlertLevel, frozenset[str]]]]:
    """Diffs the current /v1/alerts/active.json payload against previous state and returns events."""
    now = now or datetime.now(timezone.utc)
    cur_threats: dict[str, set[str]] = {}
    cur_levels: dict[str, AlertLevel] = {}

    for alert in payload.get("alerts", []):
        if alert.get("alert_type", "air_raid") != "air_raid":
            continue
        district = _match_district(alert)
        if not district:
            continue

        mapped_threats: set[str] = set()
        for t in alert.get("threats") or []:
            mapped = THREAT_TYPE_MAP.get(t.get("threat_type", ""))
            if mapped:
                mapped_threats.add(mapped)

        if "missile" in mapped_threats and ({"ballistic", "cruise_missile"} & mapped_threats):
            mapped_threats.discard("missile")

        api_level = LEVEL_MAP.get(alert.get("alert_level", ""), AlertLevel.ALERT)
        inferred = level_for_threats(mapped_threats)
        level = inferred if inferred is not AlertLevel.NONE else api_level

        cur_threats.setdefault(district, set()).update(mapped_threats)
        prev_lvl = cur_levels.get(district, AlertLevel.NONE)
        if level is AlertLevel.RED or prev_lvl is AlertLevel.NONE:
            cur_levels[district] = level
        elif level is AlertLevel.YELLOW and prev_lvl is AlertLevel.ALERT:
            cur_levels[district] = level

    new_state: dict[str, tuple[AlertLevel, frozenset[str]]] = {
        d: (cur_levels[d], frozenset(cur_threats.get(d, set())))
        for d in cur_levels
    }

    events: list[ParsedEvent] = []
    for district, (level, threats_set) in new_state.items():
        old = prev_state.get(district)
        if old == (level, threats_set):
            continue
        old_threats = old[1] if old else frozenset()
        cleared = sorted(old_threats - threats_set)
        events.append(
            ParsedEvent(
                source=OFFICIAL_SOURCE,
                message_id=0,
                timestamp=now,
                districts=[district],
                level=level,
                threats=sorted(threats_set),
                direction=None,
                is_all_clear=False,
                raw_text="official api",
                cleared_threats=cleared,
            )
        )

    for district in set(prev_state) - set(new_state):
        events.append(
            ParsedEvent(
                source=OFFICIAL_SOURCE,
                message_id=0,
                timestamp=now,
                districts=[district],
                level=AlertLevel.NONE,
                threats=[],
                direction=None,
                is_all_clear=True,
                raw_text="official api",
            )
        )

    return events, new_state


async def run_official_poller(queue: asyncio.Queue[ParsedEvent]) -> None:
    token = settings().alerts_api_token
    if not token:
        log.warning("ALERTS_API_TOKEN is not set — official alerts API poller is disabled")
        return

    headers = {"Authorization": f"Bearer {token}"}
    state: dict[str, tuple[AlertLevel, frozenset[str]]] = {}
    last_modified: str | None = None

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                req_headers = {"If-Modified-Since": last_modified} if last_modified else {}
                async with session.get(
                    API_URL,
                    headers=req_headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 304:
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    if r.status == 429:
                        log.warning("Official API rate limit hit (429), backing off for 30s")
                        await asyncio.sleep(30)
                        continue
                    r.raise_for_status()
                    last_modified = r.headers.get("Last-Modified", last_modified)
                    data = await r.json()

                events, state = parse_active_alerts(data, state)
                for ev in events:
                    await queue.put(ev)
            except Exception as e:  # noqa: BLE001
                log.warning("Official API error: %s", e)
            await asyncio.sleep(POLL_SECONDS)
