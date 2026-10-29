"""Suggests channel weights for config/channels.yaml from the bot's own history (no API calls).

Usage:
    scp root@<server>:/opt/Peace/data/peace.db data/prod.db
    python scripts/source_weights.py data/prod.db

For every alert a channel posted, it checks whether a different source reported the same zone at
the same level within WINDOW minutes (confirmed share), and how many minutes earlier than those
sources the channel was (lead). Identical texts count as one post, so reposts and duplicate ids
do not confirm themselves.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from peace.analyzer.scoring import source_weight  # noqa: E402
from peace.parser.normalizer import normalize  # noqa: E402

WINDOW = timedelta(minutes=15)
MIN_ALERTS = 10   # fewer alerts than this: not enough data to suggest a weight


@dataclass(slots=True)
class Alert:
    source: str
    ts: datetime
    district: str
    level: str
    text: str


@dataclass(slots=True)
class Score:
    alerts: int
    confirmed: float          # share of alerts confirmed by another source
    lead_min: float | None    # median minutes ahead of the first confirming source (negative = later)


def score(alerts: list[Alert]) -> dict[str, Score]:
    by_zone: dict[tuple[str, str], list[Alert]] = {}
    for a in alerts:
        by_zone.setdefault((a.district, a.level), []).append(a)
    hits: dict[str, list[bool]] = {}
    leads: dict[str, list[float]] = {}
    for a in alerts:
        others = [o for o in by_zone[(a.district, a.level)]
                  if o.source != a.source and o.text != a.text and abs(o.ts - a.ts) <= WINDOW]
        hits.setdefault(a.source, []).append(bool(others))
        if others:
            first = min(o.ts for o in others)
            leads.setdefault(a.source, []).append((first - a.ts).total_seconds() / 60)
    return {s: Score(len(h), sum(h) / len(h), statistics.median(leads[s]) if s in leads else None)
            for s, h in hits.items()}


def suggest(s: Score) -> float | None:
    """Confirmed share, kept inside the range unofficial channels use."""
    return None if s.alerts < MIN_ALERTS else round(min(0.9, max(0.3, s.confirmed)), 2)


def load(db: str) -> list[Alert]:
    rows = sqlite3.connect(db).execute(
        "SELECT source, ts, districts, level, raw_text FROM events WHERE level NOT IN ('none')").fetchall()
    return [Alert(src, datetime.fromisoformat(ts), d, level, normalize(text or ""))
            for src, ts, districts, level, text in rows for d in json.loads(districts)]


def main() -> None:
    scores = score(load(sys.argv[1]))
    print(f"{'source':24} {'alerts':>6} {'confirmed':>9} {'lead, min':>9} {'weight':>6} {'suggest':>7}")
    for src, s in sorted(scores.items(), key=lambda kv: -kv[1].confirmed):
        lead = "—" if s.lead_min is None else f"{s.lead_min:+.1f}"
        new = suggest(s)
        print(f"{src:24} {s.alerts:6} {s.confirmed:9.0%} {lead:>9} {source_weight(src):6.2f} "
              f"{'—' if new is None else f'{new:.2f}':>7}")
    print("\nOfficial channels keep their weight; change only unofficial ones in config/channels.yaml.")


if __name__ == "__main__":
    main()
