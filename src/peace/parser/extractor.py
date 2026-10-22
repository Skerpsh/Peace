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
    "рівнен", "ровен", "волин", "закарпат", "терноп", "буковин", "чернівц",
)

# Prevents "київська" / "київщина" from matching kyiv_all ("київ")
_OBLAST_ADJ_RE = re.compile(r"ки(?:їв|єв|ев)(?:ськ|ск|щин)\w*")


def _clean_lines(raw_text: str) -> str:
    """Strips channel signature/promo lines before normalizing."""
    cfg = threats()
    strip_markers = cfg.get("strip_line_markers", [])
    kept: list[str] = []
    for line in raw_text.splitlines():
        low = line.lower()
        if any(m in low for m in strip_markers):
            continue
        kept.append(line)
    return normalize("\n".join(kept))


def _is_ignored_news(text: str) -> bool:
    return any(p in text for p in threats().get("ignore_phrases", []))


def _channel_default_district(source: str, text: str) -> list[str]:
    if any(r in text for r in _OTHER_REGIONS):
        return []
    for c in channels():
        if c["username"].lower() == source.lower() and c.get("default_district"):
            return [c["default_district"]]
    return []


def _find_districts(text: str, source: str = "") -> list[str]:
    reg = regions()
    text_for_city = _OBLAST_ADJ_RE.sub(" ", text)
    found = []
    for key, info in reg.items():
        haystack = text_for_city if key == "kyiv_all" else text
        if any(alias in haystack for alias in info["aliases"]):
            found.append(key)

    # Drop kyiv_all only if a specific Kyiv City district is matched;
    # drop oblast_all only if a specific Kyiv Oblast raion is matched.
    has_city_specific = any(reg[k]["area"] == "kyiv_city" and k != "kyiv_all" for k in found)
    has_oblast_specific = any(reg[k]["area"] == "kyiv_oblast" and k != "oblast_all" for k in found)
    filtered = [
        k for k in found
        if not (k == "kyiv_all" and has_city_specific)
        and not (k == "oblast_all" and has_oblast_specific)
    ]
    return filtered or _channel_default_district(source, text)


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


def _analyze_clauses(text: str) -> tuple[list[str], list[str], bool, str, list[str]]:
    """Splits message text into clauses and separates active threats from cleared threats.

    Returns (active, cleared, bare_clear, active_text, clear_clauses):
      bare_clear    — True if a clear clause had no specific threat type (full all-clear);
      active_text   — joined clauses without clear markers;
      clear_clauses — clauses containing a bare clear marker.
    """
    cfg = threats()
    clear_markers = cfg["markers"]["clear"]
    movement_markers = cfg["markers"].get("movement", [])
    active: list[str] = []
    cleared: list[str] = []
    bare_clear = False
    active_parts: list[str] = []
    clear_clauses: list[str] = []

    for clause in re.split(cfg["clause_split"], text):
        clause = clause.strip()
        if not clause:
            continue
        found = _find_threats(clause)
        if _has_any(clause, clear_markers):
            if "червон" in clause and not found:
                found = ["missile"]
            if found:
                for t in found:
                    cleared.append(t)
                    cleared.extend(cfg["threats"][t].get("clears", []))
            else:
                bare_clear = True
                clear_clauses.append(clause)
        else:
            if not found and _has_any(clause, movement_markers):
                found = ["shahed"]
            active.extend(found)
            active_parts.append(clause)

    active = _dedup_missile(list(dict.fromkeys(active)))
    # If a threat appears as both active and cleared, keep it active for safety
    cleared = [t for t in dict.fromkeys(cleared) if t not in active]
    return active, cleared, bare_clear, " ".join(active_parts), clear_clauses


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
    text = _clean_lines(msg.text)
    if not text or _is_ignored_news(text):
        return None

    active, cleared, bare_clear, active_text, clear_clauses = _analyze_clauses(text)
    full_clear = bare_clear and not active and not cleared

    # When a message clears one district while noting another remains under alert
    # (e.g. "Київ та область — відбій! Вишгород тривога залишається"),
    # resolve districts for full_clear from the clear clause itself.
    if full_clear and clear_clauses and _fallback_level(active_text) is not AlertLevel.NONE:
        districts = _find_districts(" ".join(clear_clauses), msg.source)
    else:
        districts = _find_districts(text, msg.source)
    if not districts:
        return None

    if full_clear:
        level = AlertLevel.NONE
    elif active:
        # 1) Primary criterion — threat type
        level = level_for_threats(active)
    elif cleared:
        # Partial threat clearance only (e.g. "missile danger cleared")
        level = AlertLevel.NONE
    else:
        # 2) Fallback — explicit level phrase or generic alert keyword
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
