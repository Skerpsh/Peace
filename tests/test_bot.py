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

        pushed = [c for c, t in bot.sent if "загроза БпЛА" in t and "Статус зараз" not in t]
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
