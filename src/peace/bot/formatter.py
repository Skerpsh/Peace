"""User-facing message formatting."""
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
    AlertLevel.RED: "missile threat",
    AlertLevel.YELLOW: "UAV threat",
    AlertLevel.ALERT: "air alert, threat type pending",
}
THREAT_NAMES = {
    "ballistic": "ballistic missiles",
    "cruise_missile": "cruise missiles",
    "missile": "missiles",
    "shahed": "UAVs (Shahed)",
    "aviation": "enemy aviation activity",
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
            return f"{icon} <b>{district_name(s.district)}</b> — threat cleared ({_names(s.cleared)})"
        return f"{icon} <b>{district_name(s.district)}</b> — all clear"

    lines = [f"{icon} <b>{district_name(s.district)}</b> — {LEVEL_TEXT[s.level]}"]
    if not s.confirmed:
        lines.append("❔ <i>unconfirmed</i>")
    if s.threats:
        lines.append("Targets: " + _names(s.threats))
    if s.cleared:
        lines.append("✅ Cleared: " + _names(s.cleared))
    if s.direction:
        lines.append(f"Direction: {s.direction}")
    time = s.updated_at.astimezone(KYIV_TZ).strftime("%H:%M") if s.updated_at else "—"
    lines.append(f"<i>Confidence: {int(s.confidence * 100)}% · sources: {len(s.sources)} · {time}</i>")
    if s.updated_at:
        age_min = int((datetime.now(timezone.utc) - s.updated_at).total_seconds() // 60)
        if age_min >= settings().stale_after_minutes:
            lines.append(f"⏳ <i>no updates for {age_min} min — status may be outdated</i>")
    return "\n".join(lines)


def format_snapshot(items: list[Situation]) -> str:
    if not items:
        return "🟢 No active threats for Kyiv or Kyiv Oblast right now."
    items = sorted(items, key=lambda s: (-_RANK[s.level], s.district))
    return "\n\n".join(format_situation(s) for s in items)
