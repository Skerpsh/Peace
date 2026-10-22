"""Извлечение структурированных данных из текста сообщения (правила + словари).

Первая версия — на ключевых словах. Позже можно добавить LLM как fallback
для сообщений, где правила ничего не нашли.
"""
from __future__ import annotations

import re

from peace.config import regions, threats
from peace.models import AlertLevel, ParsedEvent, RawMessage
from peace.parser.normalizer import normalize


def _find_districts(text: str) -> list[str]:
    found = [
        key for key, info in regions().items()
        if any(alias in text for alias in info["aliases"])
    ]
    # Если найден конкретный район Киева — общий "kyiv_all" лишний
    specific = [k for k in found if not k.endswith("_all")]
    return specific or found


def _dedup_missile(found: list[str]) -> list[str]:
    """"ракета" общая — убираем, если уже есть конкретный тип ракеты."""
    if "missile" in found and ({"ballistic", "cruise_missile"} & set(found)):
        found = [t for t in found if t != "missile"]
    return found


def _find_threats(text: str) -> list[str]:
    found = []
    for key, info in threats()["threats"].items():
        for w in info["words"]:
            # короткие корни (бпла) — только как начало слова
            pattern = rf"\b{re.escape(w)}" if len(w) <= 4 else re.escape(w)
            if re.search(pattern, text):
                found.append(key)
                break
    return _dedup_missile(found)


def _has_any(text: str, markers: list[str]) -> bool:
    return any(m in text for m in markers)


def level_for_threats(found_threats: list[str] | set[str]) -> AlertLevel:
    """Уровень определяется типом угрозы: БпЛА -> жёлтый, ракеты -> красный."""
    cfg = threats()["threats"]
    levels = {AlertLevel(cfg[t]["level"]) for t in found_threats if t in cfg}
    if AlertLevel.RED in levels:
        return AlertLevel.RED
    if AlertLevel.YELLOW in levels:
        return AlertLevel.YELLOW
    return AlertLevel.NONE


def _analyze_clauses(text: str) -> tuple[list[str], list[str], bool, str]:
    """Разбивает сообщение на фразы и делит угрозы на активные и снятые.

    Возвращает (active, cleared, bare_clear, active_text):
      bare_clear  — была фраза-отбой без указания типа угрозы;
      active_text — текст фраз без маркеров снятия (для запасных маркеров).
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
    # Если тип одновременно "активен" и "снят" — безопаснее считать активным
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
    """Вернёт None, если сообщение не относится к Киеву/области или пустое по смыслу."""
    text = normalize(msg.text)
    districts = _find_districts(text)
    if not districts:
        return None

    active, cleared, bare_clear, active_text = _analyze_clauses(text)
    full_clear = bare_clear and not active and not cleared

    if full_clear:
        level = AlertLevel.NONE
    elif active:
        # 1) главный критерий — тип угрозы
        level = level_for_threats(active)
    elif cleared:
        # только снятие части угроз ("ракетна небезпека минула")
        level = AlertLevel.NONE
    else:
        # 2) запасной — явный цвет / слово "тривога"
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
