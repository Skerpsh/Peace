"""Stateful aggregation of parsed events into a per-district situation picture.

Principle: an active threat persists until new information arrives — either a
partial threat clearance ("missiles cleared") or a full all-clear.
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
# Memory bounds for a long-running process
_MAX_SEEN = 5000
_MAX_EVENTS_PER_DISTRICT = 500


class Aggregator:
    def __init__(self) -> None:
        self._events: dict[str, list[ParsedEvent]] = defaultdict(list)
        self._seen: dict[tuple[str, int, str], None] = {}  # insertion-ordered set
        self._state: dict[str, Situation] = {}

    def add(self, ev: ParsedEvent) -> list[Situation]:
        """Adds an event and returns the list of districts whose situation changed."""
        key = (ev.source, ev.message_id, ev.raw_text)
        if ev.message_id and key in self._seen:
            return []  # Duplicate
        self._seen[key] = None
        if len(self._seen) > _MAX_SEEN:
            for old in list(self._seen)[: _MAX_SEEN // 5]:
                del self._seen[old]

        changed: list[Situation] = []
        for d in ev.districts:
            self._events[d].append(ev)
            new = self._compute(d)
            old = self._state.get(d)
            if self.is_meaningful_change(old, new):
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

        # Downgrading/clearing an alert is riskier than raising one, so clearance
        # (partial or full) is accepted only from an official source or when
        # confirmed by multiple channels with combined confidence >= MIN_CONFIDENCE.
        def accepted(votes: list[ParsedEvent]) -> bool:
            return is_official(votes[-1].source) or combined_confidence(votes) >= min_conf

        support: dict[str, list[ParsedEvent]] = {}      # threat -> confirming events
        clear_votes: dict[str, list[ParsedEvent]] = {}  # threat -> clearance votes
        full_clear_votes: list[ParsedEvent] = []        # full all-clear votes
        untyped: list[ParsedEvent] = []                 # alerts without a specific threat type
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

            # Partial threat clearance ("missiles cleared, UAVs remain")
            for t in ev.cleared_threats:
                if t not in support:
                    continue
                votes = clear_votes.setdefault(t, [])
                votes.append(ev)
                if accepted(votes):
                    support.pop(t)
                    clear_votes.pop(t)
                    cleared.add(t)
                    # Drop stale untyped color markers; keep generic official ALERT status
                    untyped = [u for u in untyped if u.level is AlertLevel.ALERT]

            # Any new threat report invalidates pending unconfirmed all-clear votes
            if ev.threats or ev.level is not AlertLevel.NONE:
                full_clear_votes = []
            for t in ev.threats:
                support.setdefault(t, []).append(ev)
                clear_votes.pop(t, None)
                cleared.discard(t)

            if not ev.threats and not ev.cleared_threats and ev.level is not AlertLevel.NONE:
                untyped.append(ev)

        # Prune history prior to the latest accepted full all-clear
        if last_reset >= 0:
            evs = evs[last_reset + 1:]
        # shortcut: hard cap drops the oldest events of a very long alert; raise if alerts outlive 500 messages
        self._events[district] = evs = evs[-_MAX_EVENTS_PER_DISTRICT:]

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
        # Order red (missile) threats first, then yellow (UAV), then by confirmation count
        threat_list = sorted(
            support,
            key=lambda t: (-_LEVEL_RANK[AlertLevel(cfg[t]["level"])], -len(support[t])),
        )

        # Do not downgrade alert color on low confidence (yellow means UAV, not "unconfirmed");
        # instead mark the situation as unconfirmed via the `confirmed` flag.
        return Situation(
            district=district,
            level=level,
            threats=threat_list,
            cleared=sorted(cleared),
            direction=next((e.direction for e in reversed(evs) if e.direction and e in active), None),
            confidence=round(conf, 2),
            confirmed=conf >= min_conf,
            sources=sorted({e.source for e in active}),
            posts={e.source: e.message_id for e in sorted(active, key=lambda e: e.timestamp) if e.message_id},
            updated_at=evs[-1].timestamp,
        )

    @staticmethod
    def is_meaningful_change(old: Situation | None, new: Situation) -> bool:
        if old is None:
            return new.level is not AlertLevel.NONE
        return (
            old.level != new.level
            or old.confirmed != new.confirmed
            or set(old.threats) != set(new.threats)
            or old.direction != new.direction
        )
