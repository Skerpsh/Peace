"""User-facing message formatting (Ukrainian)."""
from __future__ import annotations

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
LEVEL_TEXT = {
    AlertLevel.RED: "ракетна небезпека",
    AlertLevel.YELLOW: "загроза БпЛА",
    AlertLevel.ALERT: "повітряна тривога, тип загрози уточнюється",
}
THREAT_NAMES = {
    "ballistic": "балістичні ракети",
    "cruise_missile": "крилаті ракети",
    "missile": "ракети",
    "shahed": "БпЛА (шахеди)",
    "aviation": "активність ворожої авіації",
}
RANK = {AlertLevel.NONE: 0, AlertLevel.ALERT: 1, AlertLevel.YELLOW: 2, AlertLevel.RED: 3}


def district_name(key: str) -> str:
    return regions().get(key, {}).get("name", key)


def _names(threats: list[str]) -> str:
    return ", ".join(THREAT_NAMES.get(t, t) for t in threats)


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


def format_situation(s: Situation) -> str:
    icon = LEVEL_ICON[s.level]
    name = escape(district_name(s.district))
    if s.level is AlertLevel.NONE:
        if s.cleared:
            return f"{icon} <b>{name}</b> — загрозу знято ({_names(s.cleared)})"
        return f"{icon} <b>{name}</b> — відбій тривоги"

    lines = [f"{icon} <b>{name}</b> — {LEVEL_TEXT[s.level]}"]
    if not s.confirmed:
        lines.append("❔ <i>не підтверджено</i>")
    if s.threats:
        lines.append("Загрози: " + _names(s.threats))
    if s.cleared:
        lines.append("✅ Знято: " + _names(s.cleared))
    if s.direction:
        lines.append(f"Напрямок: {escape(s.direction)}")
    lines.append(f"<i>Достовірність {int(s.confidence * 100)}% · {_hhmm(s.updated_at)} · {_sources(s)}</i>")
    if s.updated_at:
        age_min = int((datetime.now(timezone.utc) - s.updated_at).total_seconds() // 60)
        if age_min >= settings().stale_after_minutes:
            age = f"{age_min // 60} год" if age_min >= 120 else f"{age_min} хв"
            lines.append(f"⏳ <i>оновлень немає {age} — статус може бути застарілим</i>")
    return "\n".join(lines)


def format_snapshot(items: list[Situation]) -> str:
    if not items:
        return "🟢 Загроз для Києва та області зараз немає."
    items = sorted(items, key=lambda s: (-RANK[s.level], s.district))
    return "\n\n".join(format_situation(s) for s in items)


def format_live(items: list[Situation], now: datetime | None = None) -> str:
    """The live "status now" message that the bot keeps editing in place."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(KYIV_TZ).strftime("%H:%M:%S")
    return f"📍 <b>Статус зараз</b>\n\n{format_snapshot(items)}\n\n<i>Оновлено о {stamp}</i>"


def format_update(items: list[Situation]) -> str:
    """A push notification with the zones that just changed."""
    items = sorted(items, key=lambda s: (-RANK[s.level], s.district))
    return "\n\n".join(format_situation(s) for s in items)
