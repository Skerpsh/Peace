"""Labels real channel messages from the bot's DB with Claude and compares them with the rule parser.

Usage (needs `pip install anthropic` and ANTHROPIC_API_KEY):
    scp root@<server>:/opt/Peace/data/peace.db data/prod.db
    python scripts/label_corpus.py data/prod.db --limit 300

Prints how often rules agree with the model and writes every disagreement to
tests/corpus_review.yaml. Review it by hand: fix the expected fields where the model
is wrong, drop `rules`/`note`, and move the cases into tests/corpus.yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from peace.config import regions, threats  # noqa: E402
from peace.models import RawMessage  # noqa: E402
from peace.parser.extractor import extract  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "corpus_review.yaml"
MODEL = "claude-opus-5-5"


def _zones() -> dict[str, str]:
    return {k: v["name"] for k, v in regions().items()}


def _schema() -> dict:
    zones, kinds = list(_zones()), list(threats()["threats"])
    return {
        "type": "object",
        "properties": {
            "ignored": {"type": "boolean"},
            "districts": {"type": "array", "items": {"type": "string", "enum": zones}},
            "level": {"type": "string", "enum": ["none", "alert", "yellow", "red"]},
            "threats": {"type": "array", "items": {"type": "string", "enum": kinds}},
            "all_clear": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["ignored", "districts", "level", "threats", "all_clear", "note"],
        "additionalProperties": False,
    }


def _system() -> str:
    zones = "\n".join(f"- {k}: {name}" for k, name in _zones().items())
    kinds = "\n".join(f"- {k}: {v['level']}" for k, v in threats()["threats"].items())
    return f"""You label posts from Ukrainian Telegram monitoring channels for an air-alert bot covering Kyiv and Kyiv Oblast.

Decide what the post says about the air situation in these zones RIGHT NOW:
{zones}

Threat types and the alert level each implies:
{kinds}
Level is red if any red threat is active, else yellow if any yellow threat is active, else "alert" for an alert
of unknown type, else "none".

Rules:
- ignored=true when the post carries no current alert information for these zones: news about past attacks,
  aftermath and damage, other regions only, power outages, analytics, ads, digests. Then districts=[], level="none".
- all_clear=true only for a full all-clear in the mentioned zones (level "none", threats []).
- A partial clear ("ракети — відбій, шахеди лишаються") lists only the threats still active.
- Use kyiv_all for any part of Kyiv city. Use oblast_all for the oblast as a whole; use a raion key when a town
  of that raion is named.
- note: one short sentence explaining the decision, in Ukrainian."""


def rules_label(text: str, source: str) -> dict:
    ev = extract(RawMessage(source=source, message_id=1, text=text, date=datetime.now(timezone.utc)))
    if ev is None:
        return {"ignored": True}
    return {"districts": sorted(ev.districts), "level": ev.level.value,
            "threats": sorted(ev.threats), "all_clear": ev.is_all_clear}


def diff(expected: dict, got: dict) -> list[str]:
    """Fields where the rule result differs from the expected label."""
    if expected["ignored"] or got.get("ignored"):
        return [] if expected["ignored"] == got.get("ignored", False) else ["ignored"]
    return [f for f in ("districts", "level", "threats", "all_clear")
            if (sorted(expected[f]) if isinstance(expected[f], list) else expected[f]) != got[f]]


def load_messages(db: str, limit: int) -> list[tuple[str, str]]:
    """Newest unique (source, text) pairs, both recognised and ignored ones."""
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT source, raw_text, ts FROM events WHERE source != 'official_api'"
        " UNION ALL SELECT source, text, ts FROM ignored ORDER BY ts DESC"
    ).fetchall()
    seen: dict[str, tuple[str, str]] = {}
    for source, text, _ in rows:
        if text and text.strip() and text not in seen:
            seen[text] = (source, text)
    return list(seen.values())[:limit]


async def label(client: anthropic.AsyncAnthropic, sem: asyncio.Semaphore, system: str, schema: dict,
                text: str) -> dict | None:
    async with sem:
        try:
            r = await client.messages.create(
                model=MODEL, max_tokens=2048, system=system,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": text}],
            )
        except anthropic.APIError as e:
            print(f"skip ({type(e).__name__}): {text[:60]!r}", file=sys.stderr)
            return None
    if r.stop_reason != "end_turn":
        print(f"skip ({r.stop_reason}): {text[:60]!r}", file=sys.stderr)
        return None
    return json.loads(next(b.text for b in r.content if b.type == "text"))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    msgs = load_messages(args.db, args.limit)
    client, sem, system, schema = anthropic.AsyncAnthropic(), asyncio.Semaphore(args.concurrency), _system(), _schema()
    labels = await asyncio.gather(*(label(client, sem, system, schema, t) for _, t in msgs))

    review, stats = [], {"ok": 0, "false_alarm": 0, "missed": 0, "wrong_fields": 0}
    for (source, text), exp in zip(msgs, labels):
        if exp is None:
            continue
        got = rules_label(text, source)
        bad = diff(exp, got)
        if not bad:
            stats["ok"] += 1
            continue
        stats["false_alarm" if exp["ignored"] else "missed" if got.get("ignored") else "wrong_fields"] += 1
        case = {"text": text, "source": source}
        case |= {"ignored": True} if exp["ignored"] else {k: exp[k] for k in ("districts", "level", "threats", "all_clear")}
        review.append(case | {"rules": got, "note": exp["note"]})

    total = sum(stats.values())
    print(f"labelled {total}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if total:
        print(f"rules agree with the model on {stats['ok'] / total:.0%}")
    OUT.write_text(yaml.safe_dump(review, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    print(f"{len(review)} disagreements -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
