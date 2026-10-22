"""Structured event extraction from raw channel messages using keyword rules and dictionaries."""
from __future__ import annotations

import re

from peace.config import channels, regions, threats
from peace.models import AlertLevel, ParsedEvent, RawMessage
from peace.parser.normalizer import normalize

# Non-Kyiv oblast/city roots so local Kyiv/Bucha channels don't misattribute national news
_OTHER_REGIONS = (
    "харків", "харьков", "одес", "львів", "львов", "дніпропетров", "днепропетров",
    "запоріж", "запорож", "полтав", "миколаїв", "николаев", "херсон", "вінниц", "винниц",
)


def _channel_default_district(source: str, text: str) -> list[str]:
    if any(r in text for r in _OTHER_REGIONS):
        return []
    for c in channels():
        if c["username"].lower() == source.lower() and c.get("default_district"):
            return [c["default_district"]]
    return []


def _find_districts(text: str, source: str = "") -> list[str]:
    found = [
        key for key, info in regions().items()
        if any(alias in text for alias in info["aliases"])
    ]
    # If a specific district is matched, drop the generic city/oblast key
    specific = [k for k in found if not k.endswith("_all")]
    result = specific or found
    return result or _channel_default_district(source, text)


def _dedup_missile(found: list[str]) -> list[str]:
    """Remove generic 'missile' when a more specific missile threat is present."""
    if "missile" in found and ({"ballistic", "cruise_missile"} & set(found)):
        found = [t for t in found if t != "missile"]
    return found


def _find_threats(text: str) -> list[str]:
    found = []
    for key, info in threats()["threats"].items():
        for w in info["words"]:
            # Short roots (e.g. "бпла", "uav") must match at a word boundary
            pattern = rf"\b{re.escape(w)}" if len(w) <= 4 else re.escape(w)
            if re.search(pattern, text):
                found.append(key)
                break
    return _dedup_missile(found)


def _has_any(text: str, markers: list[str]) -> bool:
    return any(m in text for m in markers)


def level_for_threats(found_threats: list[str] | set[str]) -> AlertLevel:
    """Alert level is determined by threat type: UAV -> yellow, missiles -> red."""
    cfg = threats()["threats"]
    levels = {AlertLevel(cfg[t]["level"]) for t in found_threats if t in cfg}
    if AlertLevel.RED in levels:
        return AlertLevel.RED
    if AlertLevel.YELLOW in levels:
        return AlertLevel.YELLOW
    return AlertLevel.NONE


def _analyze_clauses(text: str) -> tuple[list[str], list[str], bool, str]:
    """Splits message text into clauses and separates active threats from cleared threats.

    Returns (active, cleared, bare_clear, active_text):
      bare_clear  — True if a clear clause had no specific threat type (full all-clear);
      active_text — joined clauses without clear markers (used for fallback level & direction).
    """
    cfg = threats()
    clear_markers = cfg["markers"]["clear"]
    active: list[str] = []
    cleared: list[str] = []
    bare_clear = False
    active_parts: list[str] = []

    for clause in re.split(cfg["clause_split"], text):
        clause = clause.strip()
        if not clause:
            continue
        found = _find_threats(clause)
        if _has_any(clause, clear_markers):
            if found:
                for t in found:
                    cleared.append(t)
                    cleared.extend(cfg["threats"][t].get("clears", []))
            else:
                bare_clear = True
        else:
            active.extend(found)
            active_parts.append(clause)

    active = _dedup_missile(list(dict.fromkeys(active)))
    # If a threat appears as both active and cleared, keep it active for safety
    cleared = [t for t in dict.fromkeys(cleared) if t not in active]
    return active, cleared, bare_clear, " ".join(active_parts)


def _fallback_level(text: str) -> AlertLevel:
    mk = threats()["markers"]
    if _has_any(text, mk["red"]):
        return AlertLevel.RED
    if _has_any(text, mk["yellow"]):
        return AlertLevel.YELLOW
    if _has_any(text, mk["alert"]):
        return AlertLevel.ALERT
    return AlertLevel.NONE


def _find_direction(text: str) -> str | None:
    for pat in threats()["direction_patterns"]:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()[:60]
    return None


def extract(msg: RawMessage) -> ParsedEvent | None:
    """Returns None if the message does not relate to Kyiv/Kyiv Oblast or carries no alert info."""
    text = normalize(msg.text)
    districts = _find_districts(text, msg.source)
    if not districts:
        return None

    active, cleared, bare_clear, active_text = _analyze_clauses(text)
    full_clear = bare_clear and not active and not cleared

    if full_clear:
        level = AlertLevel.NONE
    elif active:
        # 1) Primary criterion — threat type
        level = level_for_threats(active)
    elif cleared:
        # Partial threat clearance only (e.g. "missile danger cleared")
        level = AlertLevel.NONE
    else:
        # 2) Fallback — explicit color emoji or generic alert keyword
        level = _fallback_level(active_text)

    if level is AlertLevel.NONE and not full_clear and not cleared:
        return None

    return ParsedEvent(
        source=msg.source,
        message_id=msg.message_id,
        timestamp=msg.date,
        districts=districts,
        level=level,
        threats=active,
        direction=_find_direction(active_text) if active_text else None,
        is_all_clear=full_clear,
        raw_text=msg.text,
        cleared_threats=cleared,
    )
