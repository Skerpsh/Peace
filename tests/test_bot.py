"""Zone selection, push policy, night window, source health, storage and delivery."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from peace.analyzer.aggregator import Aggregator
from peace.bot.handlers import toggle_zone
from peace.bot.notifier import Notifier, is_night, should_push
from peace.health import DOWN_AFTER_SEC, Health
from peace.models import AlertLevel, RawMessage, Situation
from peace.parser.extractor import extract
from peace.storage.db import Database

KEYS = ["kyiv_all", "buchanskyi", "oblast_all"]


def S(level, threats=(), cleared=()):
    return Situation(district="kyiv_all", level=level, threats=list(threats), cleared=list(cleared))


def test_toggle_zone():
    assert toggle_zone(set(), "*", KEYS) == {"*"}
    assert toggle_zone({"*"}, "*", KEYS) == set()
    assert toggle_zone({"*"}, "kyiv_all", KEYS) == {"buchanskyi", "oblast_all"}
    assert toggle_zone({"kyiv_all"}, "kyiv_all", KEYS) == set()
    assert toggle_zone({"kyiv_all", "buchanskyi"}, "oblast_all", KEYS) == {"*"}


def test_push_policy():
    yellow, red = S(AlertLevel.YELLOW, ["shahed"]), S(AlertLevel.RED, ["shahed", "ballistic"])
    assert should_push(None, yellow)
    assert should_push(yellow, red)
    assert should_push(red, yellow)                       # partial clear
    assert not should_push(yellow, S(AlertLevel.YELLOW, ["shahed"]))   # e.g. only confirmation changed
    assert should_push(S(AlertLevel.RED, ["cruise_missile"]), S(AlertLevel.RED, ["cruise_missile", "ballistic"]))
    # Night mode: only red escalations and all-clears
    assert not should_push(None, yellow, quiet=True)
    assert should_push(yellow, red, quiet=True)
    assert not should_push(red, yellow, quiet=True)
    assert should_push(yellow, S(AlertLevel.NONE), quiet=True)
    assert should_push(red, S(AlertLevel.NONE, cleared=["ballistic"]), quiet=True)


def test_night_window_crosses_midnight():
    # Kyiv is UTC+2 in January; default window 23:00-07:00
    assert is_night(datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc))   # 00:30 Kyiv
    assert not is_night(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))


def test_health_down_and_recovered():
    h = Health()
    h.fail("ch", "boom")
    since = h.failing_since["ch"]
    assert h.check(since + 10) == []
    assert "не відповідає" in h.check(since + DOWN_AFTER_SEC + 1)[0]
    assert h.check(since + DOWN_AFTER_SEC + 60) == []     # notified once
    h.ok("ch")
    assert "знову працює" in h.check()[0]


def test_db_migrates_old_kyiv_districts_and_lists_recipients(tmp_path):
    async def run():
        path = str(tmp_path / "t.db")
        db = Database(path)
        await db.connect()
        await db.subscribe(1, "obolonskyi")
        await db.subscribe(1, "podilskyi")
        await db.subscribe(2, "buchanskyi")
        await db.conn.close()

        db = Database(path)
        await db.connect()
        assert await db.zones(1) == {"kyiv_all"}
        await db.set_quiet(2, True)
        rs = {r.chat_id: r for r in await db.recipients()}
        assert rs[2].quiet and rs[2].wants("buchanskyi") and not rs[2].wants("kyiv_all")
        assert rs[2].wants("oblast_all")                     # whole-oblast alert reaches raion subscribers
        assert await db.add_report(2, 7, "t", "🟡 ...")
        assert not await db.add_report(2, 7, "t", "🟡 ...")  # second press on the same push is ignored
        await db.forget(2)
        assert [r.chat_id for r in await db.recipients()] == [1]
        await db.conn.close()

    asyncio.run(run())


class FakeBot:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.sent, self.edited = [], []
        self._id = 100

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="blocked")
        self._id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        if message_id == 999:
            raise TelegramBadRequest(method=None, message="message to edit not found")
        self.edited.append((chat_id, message_id))

    async def pin_chat_message(self, *a, **kw):
        pass


def test_notifier_push_edit_and_blocked(tmp_path):
    async def run():
        db = Database(str(tmp_path / "n.db"))
        await db.connect()
        await db.subscribe(1, "*")
        await db.set_status_msg(1, 50)
        await db.subscribe(2, "buchanskyi")          # other zone: nothing delivered
        await db.subscribe(3, "*")                   # blocked the bot
        await db.subscribe(4, "kyiv_all")
        await db.set_status_msg(4, 999)              # live message was deleted

        agg = Aggregator()
        bot = FakeBot(blocked={3})
        n = Notifier(bot, db, agg)
        n.sync()

        ev = extract(RawMessage(source="official", message_id=1, text="Київ: шахеди",
                                date=datetime.now(timezone.utc)))
        await n.flush({s.district: s for s in agg.add(ev)})

        pushed = [c for c, t in bot.sent if "БпЛА" in t and "Статус зараз" not in t]
        assert sorted(pushed) == [1, 4]
        assert (1, 50) in bot.edited                  # live status edited in place
        assert any(c == 4 and "Статус зараз" in t for c, t in bot.sent)   # lost live status re-sent
        assert 2 not in [c for c, _ in bot.sent]
        assert 3 not in [r.chat_id for r in await db.recipients()]

        # Same state again is not re-delivered
        bot.sent.clear()
        await n.flush({s.district: s for s in [agg.snapshot()[0]]})
        assert bot.sent == []
        await db.conn.close()

    asyncio.run(run())


def test_escalation_and_silence():
    from peace.bot.notifier import is_escalation, is_silent
    yellow = S(AlertLevel.YELLOW, ["shahed"])
    assert is_escalation(None, yellow)
    assert is_escalation(yellow, S(AlertLevel.RED, ["shahed", "ballistic"]))
    assert is_escalation(S(AlertLevel.RED, ["cruise_missile"]), S(AlertLevel.RED, ["cruise_missile", "ballistic"]))
    assert not is_escalation(S(AlertLevel.RED, ["ballistic", "shahed"]), yellow)   # downgrade waits for the batch
    assert not is_escalation(yellow, S(AlertLevel.NONE))
    assert is_silent([yellow])                                             # unconfirmed drones
    assert not is_silent([S(AlertLevel.RED, ["ballistic"])])               # red always rings
    assert not is_silent([S(AlertLevel.NONE)])                             # all-clear rings
    confirmed = S(AlertLevel.YELLOW, ["shahed"])
    confirmed.confirmed = True
    assert not is_silent([confirmed])


def test_message_format_reads_at_a_glance():
    from datetime import timedelta
    from peace.bot.formatter import format_situation, with_start
    now = datetime(2026, 1, 1, 12, 40, tzinfo=timezone.utc)
    s = Situation(district="kyiv_all", level=AlertLevel.RED, threats=["shahed", "ballistic"],
                  confidence=0.5, sources=["mon1"], posts={"mon1": 7},
                  updated_at=now - timedelta(minutes=1), since=now - timedelta(minutes=40))
    assert format_situation(s, now).split("\n") == [
        "Статус: <b>🔴 Червоний рівень</b>",
        "Зона: <b>Київ</b>",
        "Небезпека: Балістика, БпЛА",                                  # most dangerous first
        "Траєкторія руху: невідомо",
        "Початок: 14:00 (40 хв)",
        "Достовірність: 50% ❔ ще не підтверджено",
        "Остання інформація: 14:39",
        'Джерела: <a href="https://t.me/mon1/7">mon1</a>',
    ]
    assert "УКРИТТ" not in format_situation(s, now).upper()           # no instructions to act

    clear = with_start(S(AlertLevel.NONE), s)
    clear.updated_at = now + timedelta(minutes=32)
    assert format_situation(clear, now).split("\n") == [
        "Статус: <b>🟢 Відбій тривоги</b>", "Зона: Київ", "Тривала: 14:00–15:12 (1 год 12 хв)"]


def test_same_state_zones_share_one_card():
    from peace.bot.formatter import format_snapshot
    now = datetime(2026, 1, 1, 12, 40, tzinfo=timezone.utc)

    def zone(d, level, threats, conf, src):
        return Situation(district=d, level=level, threats=threats, confidence=conf, confirmed=conf >= 0.6,
                         sources=[src], updated_at=now, since=now)

    text = format_snapshot([
        zone("buchanskyi", AlertLevel.YELLOW, ["shahed"], 0.84, "mon1"),
        zone("kyiv_all", AlertLevel.RED, ["ballistic"], 1.0, "official"),
        zone("brovarskyi", AlertLevel.YELLOW, ["shahed"], 0.5, "mon2"),
    ], now)
    red, yellow = text.split("\n\n")
    assert "Червоний" in red and "Київ" in red
    assert "Зона: <b>Броварський р-н, Бучанський р-н</b>" in yellow
    assert "Достовірність: 50% ❔" in yellow                           # weakest zone sets confidence
    assert "Джерела: mon1, mon2" in yellow


def test_red_pushes_go_first_and_delay_is_recorded(tmp_path):
    async def run():
        db = Database(str(tmp_path / "p.db"))
        await db.connect()
        await db.subscribe(1, "buchanskyi")          # gets only yellow
        await db.subscribe(2, "kyiv_all")            # gets red
        agg = Aggregator()
        bot = FakeBot()
        n = Notifier(bot, db, agg)
        n.sync()
        now = datetime.now(timezone.utc)
        changes = []
        for text in ("Буча: шахеди", "Київ: балістика"):
            changes += agg.add(extract(RawMessage(source="official", message_id=len(changes) + 1,
                                                  text=text, date=now)))
        await n.flush({s.district: s for s in changes})
        pushes = [c for c, t in bot.sent if "Статус зараз" not in t]
        assert pushes == [2, 1]
        assert len(n.delays) == 2 and all(d < 5 for _, d in n.delays)
        await db.conn.close()

    asyncio.run(run())


def test_old_posts_reread_by_the_poller_are_not_news():
    from datetime import timedelta
    from peace.collector.telegram_listener import is_stale
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert is_stale(now - timedelta(hours=10), now)        # this morning's alert after a restart
    assert not is_stale(now - timedelta(minutes=5), now)
