from datetime import datetime, timedelta, timezone

from peace.analyzer.aggregator import Aggregator
from peace.models import AlertLevel, RawMessage
from peace.parser.extractor import extract

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)  # заведомо в прошлом
_id = iter(range(1, 10_000))


def feed(agg: Aggregator, source: str, text: str, minute: int):
    ev = extract(RawMessage(source=source, message_id=next(_id), text=text,
                            date=T0 + timedelta(minutes=minute)))
    assert ev is not None, text
    return agg.add(ev)


def kyiv(agg: Aggregator):
    return next((s for s in agg.snapshot() if s.district == "kyiv_all"), None)


def test_red_plus_list_when_missiles_and_drones():
    agg = Aggregator()
    feed(agg, "official", "Київ — шахеди з півночі", 0)
    feed(agg, "official", "Київ — крилаті ракети", 1)
    s = kyiv(agg)
    assert s.level is AlertLevel.RED
    assert s.threats == ["cruise_missile", "shahed"]  # ракеты первыми


def test_threat_holds_until_new_info_and_marked_stale():
    from peace.bot.formatter import format_situation

    agg = Aggregator()
    feed(agg, "mon1", "Київ — шахеди з півночі", 0)  # T0 — давно в прошлом
    s = kyiv(agg)
    # угроза никуда не делась, доверие не упало со временем
    assert s.level is AlertLevel.YELLOW
    assert s.confidence == 0.5
    assert "без оновлень" in format_situation(s)


def test_official_partial_clear_red_to_yellow():
    agg = Aggregator()
    feed(agg, "official", "Київ: крилаті ракети та шахеди", 0)
    changed = feed(agg, "official", "Київ: загроза ракет минула, БпЛА лишаються", 5)
    assert changed and changed[0].level is AlertLevel.YELLOW
    s = kyiv(agg)
    assert s.level is AlertLevel.YELLOW
    assert s.threats == ["shahed"]
    assert "cruise_missile" in s.cleared


def test_single_unofficial_partial_clear_is_not_enough():
    agg = Aggregator()
    feed(agg, "official", "Київ: балістика та шахеди", 0)
    feed(agg, "mon1", "Київ: ракетна небезпека минула", 1)
    assert kyiv(agg).level is AlertLevel.RED      # 0.5 < 0.6 — не верим
    feed(agg, "mon2", "Київ: ракети минули, дрони ще в небі", 2)
    assert kyiv(agg).level is AlertLevel.YELLOW   # 1-(0.5*0.5)=0.75 — подтверждено


def test_new_missile_after_partial_clear_returns_red():
    agg = Aggregator()
    feed(agg, "official", "Київ: крилаті ракети та шахеди", 0)
    feed(agg, "official", "Київ: загроза ракет минула, БпЛА лишаються", 5)
    feed(agg, "official", "Київ: загроза балістики!", 8)
    s = kyiv(agg)
    assert s.level is AlertLevel.RED
    assert "ballistic" in s.threats


def test_official_full_clear_resets_and_prunes():
    agg = Aggregator()
    feed(agg, "official", "Київ: шахеди", 0)
    feed(agg, "official", "🟢 Відбій тривоги у м. Київ", 10)
    assert kyiv(agg) is None
    assert agg._events["kyiv_all"] == []  # история до отбоя очищена


def test_unofficial_full_clear_needs_consensus():
    agg = Aggregator()
    feed(agg, "mon1", "Київ: шахеди", 0)
    feed(agg, "mon1", "Відбій тривоги, Київ", 30)
    assert kyiv(agg) is not None          # один канал с весом 0.5 — мало
    feed(agg, "mon2", "Київ — відбій", 31)
    assert kyiv(agg) is None              # 0.75 >= 0.6 — отбой принят


def test_new_threat_cancels_pending_clear_votes():
    agg = Aggregator()
    feed(agg, "official", "Київ: шахеди", 0)
    feed(agg, "mon1", "Відбій тривоги, Київ", 10)
    feed(agg, "official", "Київ: нова група шахедів", 11)
    feed(agg, "mon2", "Київ — відбій", 12)
    assert kyiv(agg) is not None          # голос mon1 устарел после новой угрозы
