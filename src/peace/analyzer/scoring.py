"""Расчёт доверия к информации.

Идея: каждый источник имеет базовый вес; подтверждения из разных независимых
источников складываются по формуле P = 1 - Π(1 - w_i), т.е. два источника
по 0.6 дают 0.84.

Затухания по времени НЕТ: угроза держится до новой информации (снятие / отбой),
а «давность» показывается пользователю отдельно.
"""
from __future__ import annotations

from peace.config import channels
from peace.models import ParsedEvent

OFFICIAL_SOURCE = "official_api"


def source_weight(source: str) -> float:
    if source == OFFICIAL_SOURCE:
        return 1.0
    for c in channels():
        if c["username"].lower() == source.lower():
            return float(c["weight"])
    return 0.3  # неизвестный источник


def is_official(source: str) -> bool:
    if source == OFFICIAL_SOURCE:
        return True
    return any(c["username"].lower() == source.lower() and c.get("official") for c in channels())


def combined_confidence(events: list[ParsedEvent]) -> float:
    """Каждый источник учитывается один раз, сколько бы сообщений он ни прислал."""
    p_wrong = 1.0
    for source in {ev.source for ev in events}:
        p_wrong *= 1.0 - source_weight(source)
    return 1.0 - p_wrong
