"""User-facing message formatting (Ukrainian).

Every card has the same labelled lines (status, zones, threats, start, confidence, last update,
sources), and zones in the same state share one card so a wide attack stays short.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from peace.analyzer.scoring import OFFICIAL_SOURCE
from peace.config import regions, settings
from peace.models import AlertLevel, Situation

KYIV_TZ = ZoneInfo("Europe/Kyiv")

LEVEL_ICON = {
    AlertLevel.RED: "🔴",
    AlertLevel.YELLOW: "🟡",
    AlertLevel.ALERT: "⚠️",
    AlertLevel.NONE: "🟢",
}
LEVEL_NAME = {
    AlertLevel.RED: "🔴 Червоний рівень",
    AlertLevel.YELLOW: "🟡 Жовтий рівень",
    AlertLevel.ALERT: "⚠️ Повітряна тривога, тип уточнюється",
}
# Most dangerous first
THREAT_ORDER = ["ballistic", "cruise_missile", "missile", "aviation", "shahed"]
THREAT_NAMES = {
    "ballistic": "Балістика",
    "cruise_missile": "Крилаті ракети",
    "missile": "Ракети",
    "aviation": "Зліт авіації (МіГ-31К / Ту-95)",
    "shahed": "БпЛА",
}
RANK = {AlertLevel.NONE: 0, AlertLevel.ALERT: 1, AlertLevel.YELLOW: 2, AlertLevel.RED: 3}


def district_name(key: str) -> str:
    return regions().get(key, {}).get("name", key)


def _names(threats: list[str]) -> str:
    return ", ".join(THREAT_NAMES.get(t, t) for t in threats)


def _ordered(threats: list[str]) -> list[str]:
    return sorted(threats, key=lambda t: THREAT_ORDER.index(t) if t in THREAT_ORDER else len(THREAT_ORDER))


def _sources(group: list[Situation]) -> str:
    posts = {src: mid for s in group for src, mid in s.posts.items()}
    parts = []
    for src in sorted({src for s in group for src in s.sources}):
        if src == OFFICIAL_SOURCE:
            parts.append("alerts.in.ua")
        elif src in posts:
            parts.append(f'<a href="https://t.me/{escape(src)}/{posts[src]}">{escape(src)}</a>')
        else:
            parts.append(escape(src))
    return ", ".join(parts) or "—"


def _hhmm(dt: datetime | None) -> str:
    return dt.astimezone(KYIV_TZ).strftime("%H:%M") if dt else "—"


def duration(start: datetime, end: datetime) -> str:
    mins = int((end - start).total_seconds() // 60)
    if mins < 1:
        return "щойно"
    if mins < 60:
        return f"{mins} хв"
    return f"{mins // 60} год {mins % 60} хв" if mins % 60 else f"{mins // 60} год"


def _zones(group: list[Situation]) -> str:
    return escape(", ".join(district_name(s.district) for s in group))


def format_group(group: list[Situation], now: datetime | None = None) -> str:
    """One card for zones that share the same level and threats."""
    now = now or datetime.now(timezone.utc)
    first = group[0]
    since = min((s.since for s in group if s.since), default=None)
    updated = max((s.updated_at for s in group if s.updated_at), default=None)
    cleared = _ordered(list({t for s in group for t in s.cleared}))

    if first.level is AlertLevel.NONE:
        lines = [f"Статус: <b>🟢 {'Загрозу знято' if cleared else 'Відбій тривоги'}</b>",
                 f"Зона: {_zones(group)}"]
        if cleared:
            lines.append(f"Минуло: {_names(cleared)}")
        if since and updated:
            lines.append(f"Тривала: {_hhmm(since)}–{_hhmm(updated)} ({duration(since, updated)})")
        return "\n".join(lines)

    confidence = min(s.confidence for s in group)
    confirmed = all(s.confirmed for s in group)
    lines = [
        f"Статус: <b>{LEVEL_NAME[first.level]}</b>",
        f"Зона: <b>{_zones(group)}</b>",
        f"Небезпека: {_names(_ordered(first.threats)) or 'уточнюється'}",
        "Траєкторія руху: " + escape("; ".join(sorted({s.direction for s in group if s.direction})) or "невідомо"),
        f"Початок: {_hhmm(since)} ({duration(since, now)})" if since else "Початок: —",
        f"Достовірність: {int(confidence * 100)}%" + ("" if confirmed else " ❔ ще не підтверджено"),
        f"Остання інформація: {_hhmm(updated)}",
    ]
    if updated and (now - updated).total_seconds() // 60 >= settings().stale_after_minutes:
        lines.append(f"⏳ оновлень немає {duration(updated, now)} — статус може бути застарілим")
    if cleared:
        lines.append(f"✅ Минуло: {_names(cleared)}")
    lines.append(f"Джерела: {_sources(group)}")
    return "\n".join(lines)


def format_situation(s: Situation, now: datetime | None = None) -> str:
    return format_group([s], now)


def _grouped(items: list[Situation], now: datetime | None = None) -> str:
    """Most dangerous first; zones with the same level, threats and clears share one card."""
    groups: dict[tuple, list[Situation]] = {}
    for s in sorted(items, key=lambda s: (-RANK[s.level], s.district)):
        groups.setdefault((s.level, tuple(sorted(s.threats)), tuple(sorted(s.cleared))), []).append(s)
    return "\n\n".join(format_group(g, now) for g in groups.values())


def format_snapshot(items: list[Situation], now: datetime | None = None) -> str:
    if not items:
        return "🟢 Загроз для Києва та області зараз немає."
    return _grouped(items, now)


def format_live(items: list[Situation], now: datetime | None = None) -> str:
    """The live "status now" message that the bot keeps editing in place."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(KYIV_TZ).strftime("%H:%M:%S")
    return f"📍 <b>Статус зараз</b>\n\n{format_snapshot(items, now)}\n\n<i>Оновлено о {stamp}</i>"


def format_update(items: list[Situation]) -> str:
    """A push notification with the zones that just changed."""
    return _grouped(items)


def with_start(new: Situation, old: Situation | None) -> Situation:
    """An all-clear keeps the start of the alert it ends, so it can say how long the alert lasted."""
    if new.level is AlertLevel.NONE and old and old.since:
        return replace(new, since=old.since)
    return new
