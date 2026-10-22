"""Модели данных, которые проходят через конвейер: Raw -> Parsed -> Situation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AlertLevel(str, Enum):
    """Механика Киева: цвет зависит от ТИПА угрозы."""
    NONE = "none"
    ALERT = "alert"     # тревога объявлена, тип угрозы пока не известен
    YELLOW = "yellow"   # угроза БпЛА (шахеды)
    RED = "red"         # ракетная угроза (крылатые, баллистика)


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
    districts: list[str]            # ключи из regions.yaml
    level: AlertLevel
    threats: list[str]              # ключи из threats.yaml
    direction: str | None
    is_all_clear: bool              # полный отбой тревоги
    raw_text: str
    # угрозы, которые сообщение СНИМАЕТ без полного отбоя
    # ("загроза ракет минула, БпЛА лишаються" -> ["missile", ...])
    cleared_threats: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Situation:
    """Текущая агрегированная картина по одному району."""
    district: str
    level: AlertLevel
    threats: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)   # угрозы, снятые в ходе этой тревоги
    direction: str | None = None
    confidence: float = 0.0
    confirmed: bool = False         # доверие >= MIN_CONFIDENCE
    sources: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
