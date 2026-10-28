"""Admin monitoring: alarms, problem detection, reports."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import KYIV_TZ
from peace.health import Health
from peace.models import RawMessage
from peace.monitor import Alarms, Monitor, problems
from peace.storage.db import Database

OK = {"disk_free_gb": 50, "disk_free_pct": 60, "mem_free_pct": 40}


def test_alarm_after_two_checks_once_and_recovery():
    a = Alarms()
    assert a.update({"disk": "low"}) == []
    assert a.update({"disk": "low"}) == ["🚨 low"]
    assert a.update({"disk": "low"}) == []
    assert a.update({}) == ["✅ Минуло: low"]
    assert a.update({"disk": "low"}) == []      # a one-off blip does not alarm


def test_problems():
    assert problems(OK, 5, 26) == {}
    assert set(problems({**OK, "disk_free_gb": 0.5}, None, 26)) == {"disk"}
    assert set(problems({**OK, "mem_free_pct": 2}, None, 26)) == {"memory"}
    assert set(problems(OK, 30, 26)) == {"backup"}


def test_report_and_daily_summary(tmp_path):
    async def run():
        db = Database(str(tmp_path / "m.db"))
        await db.connect()
        await db.subscribe(1, "*")
        await db.set_quiet(1, True)
        now = datetime.now(timezone.utc)
        await db.log_ignored(RawMessage(source="ch", message_id=1, text="news", date=now))
        notifier = SimpleNamespace(counters=Counter(push=3), ready=True)
        mon = Monitor(None, db, Aggregator(), notifier, Health())
        mon._prev_report = now - timedelta(minutes=15)
        mon._prev_counters = {}                      # the 3 pushes happened during this period
        try:
            await check(db, mon, now)
        finally:
            await db.conn.close()

    async def check(db, mon, now):

        text = await mon.report()
        assert "Підписники: 1" in text and "🌙 1" in text
        assert "нерозпізнаних 1" in text and "пушів 3" in text

        yesterday = (now.astimezone(KYIV_TZ) - timedelta(days=1)).date().isoformat()
        await db.save_daily(yesterday, {"subscribers": 0, "events": 0, "ignored": 4, "pushes": 1})
        summary = await mon.daily_summary(now)
        assert "Підписники: 1 (▲1)" in summary and "Нерозпізнаних: 1 (▼3)" in summary
        assert mon._day_push_base == 3                # next day's pushes start from here

    asyncio.run(run())
