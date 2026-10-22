"""Форматирование ситуации для пользователя."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
    AlertLevel.RED: "ракетна загроза",
    AlertLevel.YELLOW: "загроза БпЛА",
    AlertLevel.ALERT: "тривога, тип загрози уточнюється",
}
THREAT_NAMES = {
    "ballistic": "балістика",
    "cruise_missile": "крилаті ракети",
    "missile": "ракети",
    "shahed": "БпЛА (шахеди)",
    "aviation": "активність авіації",
}
_RANK = {AlertLevel.NONE: 0, AlertLevel.ALERT: 1, AlertLevel.YELLOW: 2, AlertLevel.RED: 3}


def district_name(key: str) -> str:
    return regions().get(key, {}).get("name", key)


def _names(threats: list[str]) -> str:
    return ", ".join(THREAT_NAMES.get(t, t) for t in threats)


def format_situation(s: Situation) -> str:
    icon = LEVEL_ICON[s.level]
    if s.level is AlertLevel.NONE:
        if s.cleared:
            return f"{icon} <b>{district_name(s.district)}</b> — загроза минула ({_names(s.cleared)})"
        return f"{icon} <b>{district_name(s.district)}</b> — відбій"

    lines = [f"{icon} <b>{district_name(s.district)}</b> — {LEVEL_TEXT[s.level]}"]
    if not s.confirmed:
        lines.append("❔ <i>не підтверджено</i>")
    if s.threats:
        lines.append("Цілі: " + _names(s.threats))
    if s.cleared:
        lines.append("✅ Минула: " + _names(s.cleared))
    if s.direction:
        lines.append(f"Напрямок: {s.direction}")
    time = s.updated_at.astimezone(KYIV_TZ).strftime("%H:%M") if s.updated_at else "—"
    lines.append(f"<i>Довіра: {int(s.confidence * 100)}% · джерел: {len(s.sources)} · {time}</i>")
    if s.updated_at:
        age_min = int((datetime.now(timezone.utc) - s.updated_at).total_seconds() // 60)
        if age_min >= settings().stale_after_minutes:
            lines.append(f"⏳ <i>без оновлень {age_min} хв — інформація може бути неактуальною</i>")
    return "\n".join(lines)


def format_snapshot(items: list[Situation]) -> str:
    if not items:
        return "🟢 Зараз загроз для Києва та області не зафіксовано."
    items = sorted(items, key=lambda s: (-_RANK[s.level], s.district))
    return "\n\n".join(format_situation(s) for s in items)
