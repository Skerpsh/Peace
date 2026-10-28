"""User-facing message formatting (Ukrainian).

Built to be read in two seconds: the first line (what a locked phone shows) says where and what,
the second how long and whether it is confirmed; technical details are folded away.
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
# Headline when a level is known but no specific threat type is
LEVEL_HEAD = {
    AlertLevel.RED: "РАКЕТНА НЕБЕЗПЕКА",
    AlertLevel.YELLOW: "ЗАГРОЗА БпЛА",
    AlertLevel.ALERT: "ПОВІТРЯНА ТРИВОГА",
}
# Most dangerous first: the first threat sets the icon
THREAT_ORDER = ["ballistic", "cruise_missile", "missile", "aviation", "shahed"]
THREAT_ICON = {"ballistic": "🚀", "cruise_missile": "🔴", "missile": "🔴", "aviation": "✈️", "shahed": "🟡"}
THREAT_HEAD = {
    "ballistic": "БАЛІСТИКА",
    "cruise_missile": "КРИЛАТІ РАКЕТИ",
    "missile": "РАКЕТИ",
    "aviation": "ЗЛІТ АВІАЦІЇ",
    "shahed": "ДРОНИ",
}
THREAT_NAMES = {
    "ballistic": "балістика",
    "cruise_missile": "крилаті ракети",
    "missile": "ракети",
    "aviation": "зліт авіації (МіГ-31К / Ту-95)",
    "shahed": "дрони",
}
RANK = {AlertLevel.NONE: 0, AlertLevel.ALERT: 1, AlertLevel.YELLOW: 2, AlertLevel.RED: 3}


def district_name(key: str) -> str:
    return regions().get(key, {}).get("name", key)


def _names(threats: list[str]) -> str:
    return ", ".join(THREAT_NAMES.get(t, t) for t in threats)


def _ordered(threats: list[str]) -> list[str]:
    return sorted(threats, key=lambda t: THREAT_ORDER.index(t) if t in THREAT_ORDER else len(THREAT_ORDER))


def _sources(s: Situation) -> str:
    parts = []
    for src in s.sources:
        if src == OFFICIAL_SOURCE:
            parts.append("alerts.in.ua")
        elif src in s.posts:
            parts.append(f'<a href="https://t.me/{escape(src)}/{s.posts[src]}">{escape(src)}</a>')
        else:
            parts.append(escape(src))
    return ", ".join(parts)


def _hhmm(dt: datetime | None) -> str:
    return dt.astimezone(KYIV_TZ).strftime("%H:%M") if dt else "—"


def duration(start: datetime, end: datetime) -> str:
    mins = int((end - start).total_seconds() // 60)
    if mins < 1:
        return "щойно"
    if mins < 60:
        return f"{mins} хв"
    return f"{mins // 60} год {mins % 60} хв" if mins % 60 else f"{mins // 60} год"


def headline(s: Situation) -> str:
    """The first line: icon, zone, threats."""
    name = escape(district_name(s.district))
    if s.level is AlertLevel.NONE:
        return f"🟢 <b>{name}: ЗАГРОЗУ ЗНЯТО</b>" if s.cleared else f"🟢 <b>{name}: ВІДБІЙ ТРИВОГИ</b>"
    threats = _ordered(s.threats)
    if threats:
        return f"{THREAT_ICON.get(threats[0], LEVEL_ICON[s.level])} <b>{name}: " \
               f"{' + '.join(THREAT_HEAD.get(t, t) for t in threats)}</b>"
    return f"{LEVEL_ICON[s.level]} <b>{name}: {LEVEL_HEAD[s.level]}</b>"


def format_situation(s: Situation, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [headline(s)]

    if s.level is AlertLevel.NONE:
        if s.cleared:
            lines.append(f"Минуло: {_names(_ordered(s.cleared))}")
        if s.since and s.updated_at:
            lines.append(f"Тривога тривала {duration(s.since, s.updated_at)} · {_hhmm(s.updated_at)}")
        return "\n".join(lines)

    status = [] if s.confirmed else ["❔ попередньо"]
    if s.since:
        status.append(f"з {_hhmm(s.since)}, {duration(s.since, now)}")
    if status:
        lines.append(" · ".join(status))
    if s.cleared:
        lines.append(f"✅ Минуло: {_names(_ordered(s.cleared))}")
    if s.updated_at:
        age_min = int((now - s.updated_at).total_seconds() // 60)
        if age_min >= settings().stale_after_minutes:
            lines.append(f"⏳ оновлень немає {duration(s.updated_at, now)} — статус може бути застарілим")

    details = []
    if s.direction:
        details.append(f"Напрямок: {escape(s.direction)}")
    details.append(f"Джерела: {_sources(s)} · достовірність {int(s.confidence * 100)}%")
    lines.append("<blockquote expandable>" + "\n".join(details) + "</blockquote>")
    return "\n".join(lines)


def _sorted(items: list[Situation]) -> list[Situation]:
    return sorted(items, key=lambda s: (-RANK[s.level], s.district))


def format_snapshot(items: list[Situation]) -> str:
    if not items:
        return "🟢 Загроз для Києва та області зараз немає."
    return "\n\n".join(format_situation(s) for s in _sorted(items))


def format_live(items: list[Situation], now: datetime | None = None) -> str:
    """The live "status now" message that the bot keeps editing in place."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(KYIV_TZ).strftime("%H:%M:%S")
    return f"📍 <b>Статус зараз</b>\n\n{format_snapshot(items)}\n\n<i>Оновлено о {stamp}</i>"


def format_update(items: list[Situation]) -> str:
    """A push notification with the zones that just changed."""
    return "\n\n".join(format_situation(s) for s in _sorted(items))


def with_start(new: Situation, old: Situation | None) -> Situation:
    """An all-clear keeps the start of the alert it ends, so it can say how long the alert lasted."""
    if new.level is AlertLevel.NONE and old and old.since:
        return replace(new, since=old.since)
    return new
