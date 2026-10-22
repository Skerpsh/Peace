"""Сведение потока событий в актуальную картину по районам.

Принцип: угроза держится, пока не придёт НОВАЯ информация —
снятие конкретной угрозы («ракети минули») или полный отбой.
Таймеров истечения нет.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from peace.analyzer.scoring import combined_confidence, is_official
from peace.config import settings
from peace.config import threats as threats_cfg
from peace.models import AlertLevel, ParsedEvent, Situation
from peace.parser.extractor import level_for_threats

_LEVEL_RANK = {AlertLevel.NONE: 0, AlertLevel.ALERT: 1, AlertLevel.YELLOW: 2, AlertLevel.RED: 3}


class Aggregator:
    def __init__(self) -> None:
        self._events: dict[str, list[ParsedEvent]] = defaultdict(list)
        self._seen: set[tuple[str, int, str]] = set()
        self._state: dict[str, Situation] = {}

    def add(self, ev: ParsedEvent) -> list[Situation]:
        """Добавляет событие, возвращает список районов, где картина ИЗМЕНИЛАСЬ."""
        key = (ev.source, ev.message_id, ev.raw_text)
        if ev.message_id and key in self._seen:
            return []  # дубль
        self._seen.add(key)

        changed: list[Situation] = []
        for d in ev.districts:
            self._events[d].append(ev)
            new = self._compute(d)
            old = self._state.get(d)
            if self._is_meaningful_change(old, new):
                changed.append(new)
            self._state[d] = new
        return changed

    def snapshot(self) -> list[Situation]:
        result = [self._compute(d) for d in list(self._events)]
        return [s for s in result if s.level is not AlertLevel.NONE]

    # --- internal ---------------------------------------------------------

    def _compute(self, district: str) -> Situation:
        evs = sorted(self._events[district], key=lambda e: e.timestamp)
        min_conf = settings().min_confidence

        # Понижение уровня опаснее повышения, поэтому снятие (частичное или полное)
        # применяем, только если источник официальный или его подтвердили
        # несколько каналов с суммарным доверием >= MIN_CONFIDENCE.
        def accepted(votes: list[ParsedEvent]) -> bool:
            return is_official(votes[-1].source) or combined_confidence(votes) >= min_conf

        support: dict[str, list[ParsedEvent]] = {}      # угроза -> подтверждающие события
        clear_votes: dict[str, list[ParsedEvent]] = {}  # угроза -> сообщения о её снятии
        full_clear_votes: list[ParsedEvent] = []        # сообщения о полном отбое
        untyped: list[ParsedEvent] = []                 # тревога без указания типа угрозы
        cleared: set[str] = set()
        last_reset = -1

        for i, ev in enumerate(evs):
            if ev.is_all_clear:
                full_clear_votes.append(ev)
                if accepted(full_clear_votes):
                    support.clear(); clear_votes.clear(); untyped.clear(); cleared.clear()
                    full_clear_votes = []
                    last_reset = i
                continue

            # Частичное снятие ("ракети минули, БпЛА лишаються")
            for t in ev.cleared_threats:
                if t not in support:
                    continue
                votes = clear_votes.setdefault(t, [])
                votes.append(ev)
                if accepted(votes):
                    support.pop(t)
                    clear_votes.pop(t)
                    cleared.add(t)
                    # старые "🔴/🟡 без типа" устарели; официальная тревога (ALERT) остаётся
                    untyped = [u for u in untyped if u.level is AlertLevel.ALERT]

            # Новая информация об угрозе отменяет незавершённые голоса за отбой
            if ev.threats or ev.level is not AlertLevel.NONE:
                full_clear_votes = []
            for t in ev.threats:
                support.setdefault(t, []).append(ev)
                clear_votes.pop(t, None)
                cleared.discard(t)

            if not ev.threats and not ev.cleared_threats and ev.level is not AlertLevel.NONE:
                untyped.append(ev)

        # Всё до последнего принятого отбоя больше не нужно
        if last_reset >= 0:
            evs = evs[last_reset + 1:]
        self._events[district] = evs

        if support:
            level = level_for_threats(set(support))
            active = [e for group in support.values() for e in group] + untyped
        elif untyped:
            level = max((e.level for e in untyped), key=_LEVEL_RANK.__getitem__)
            active = untyped
        else:
            return Situation(district=district, level=AlertLevel.NONE, cleared=sorted(cleared),
                             updated_at=evs[-1].timestamp if evs else datetime.now(timezone.utc))

        conf = combined_confidence(active)
        cfg = threats_cfg()["threats"]
        # Сначала ракетные (red), потом БпЛА; внутри — по числу подтверждений
        threat_list = sorted(
            support,
            key=lambda t: (-_LEVEL_RANK[AlertLevel(cfg[t]["level"])], -len(support[t])),
        )

        # Цвет НЕ понижаем при низком доверии (жёлтый = БпЛА, это другой смысл),
        # а помечаем ситуацию как неподтверждённую.
        return Situation(
            district=district,
            level=level,
            threats=threat_list,
            cleared=sorted(cleared),
            direction=next((e.direction for e in reversed(evs) if e.direction and e in active), None),
            confidence=round(conf, 2),
            confirmed=conf >= min_conf,
            sources=sorted({e.source for e in active}),
            # время последней информации по району (включая частичные снятия)
            updated_at=evs[-1].timestamp,
        )

    @staticmethod
    def _is_meaningful_change(old: Situation | None, new: Situation) -> bool:
        if old is None:
            return new.level is not AlertLevel.NONE
        return (
            old.level != new.level
            or set(old.threats) != set(new.threats)
            or old.direction != new.direction
        )
