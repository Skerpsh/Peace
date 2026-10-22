"""Source trust and multi-source confidence calculation.

Each source has a base trust weight; independent confirmations combine via
P = 1 - Π(1 - w_i), so two sources with weight 0.6 yield 0.84.

There is no time decay: threats remain active until new information arrives
(partial clear or all-clear), while message age is displayed separately.
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
    return 0.3  # Unknown source


def is_official(source: str) -> bool:
    if source == OFFICIAL_SOURCE:
        return True
    return any(c["username"].lower() == source.lower() and c.get("official") for c in channels())


def combined_confidence(events: list[ParsedEvent]) -> float:
    """Each unique source contributes once regardless of how many messages it sent."""
    p_wrong = 1.0
    for source in {ev.source for ev in events}:
        p_wrong *= 1.0 - source_weight(source)
    return 1.0 - p_wrong
