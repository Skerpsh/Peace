"""Data models flowing through the pipeline: RawMessage -> ParsedEvent -> Situation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AlertLevel(str, Enum):
    """Kyiv alert mechanics: alert color is determined by the threat type."""
    NONE = "none"
    ALERT = "alert"     # Air alert declared, threat type not yet identified
    YELLOW = "yellow"   # UAV threat (Shahed / reconnaissance drones)
    RED = "red"         # Missile threat (cruise, ballistic, MiG-31K)


@dataclass(slots=True)
class RawMessage:
    source: str
    message_id: int
    text: str
    date: datetime
    edited: bool = False


@dataclass(slots=True)
class ParsedEvent:
    source: str
    message_id: int
    timestamp: datetime
    districts: list[str]            # Keys from regions.yaml
    level: AlertLevel
    threats: list[str]              # Keys from threats.yaml
    direction: str | None
    is_all_clear: bool              # Full all-clear for the district
    raw_text: str
    # Threat types cleared without a full all-clear
    # (e.g. "missile threat cleared, UAVs remain" -> ["missile", ...])
    cleared_threats: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Situation:
    """Current aggregated state for a single district."""
    district: str
    level: AlertLevel
    threats: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)   # Threats cleared during the current alert
    direction: str | None = None
    confidence: float = 0.0
    confirmed: bool = False         # True when confidence >= MIN_CONFIDENCE
    sources: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
