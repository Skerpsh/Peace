from datetime import datetime, timedelta, timezone

from peace.analyzer.aggregator import Aggregator
from peace.models import AlertLevel, RawMessage
from peace.parser.extractor import extract

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)  # Fixed timestamp in the past
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
    assert s.threats == ["cruise_missile", "shahed"]  # Missiles listed first


def test_threat_holds_until_new_info_and_marked_stale():
    from peace.bot.formatter import format_situation

    agg = Aggregator()
    feed(agg, "mon1", "Київ — шахеди з півночі", 0)  # T0 is far in the past
    s = kyiv(agg)
    # Threat remains active and confidence does not decay over time
    assert s.level is AlertLevel.YELLOW
    assert s.confidence == 0.5
    assert "оновлень немає" in format_situation(s)


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
    assert kyiv(agg).level is AlertLevel.RED      # 0.5 < 0.6 — unconfirmed clearance ignored
    feed(agg, "mon2", "Київ: ракети минули, дрони ще в небі", 2)
    assert kyiv(agg).level is AlertLevel.YELLOW   # 1 - (0.5 * 0.5) = 0.75 — confirmed


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
    assert agg._events["kyiv_all"] == []  # History prior to all-clear is pruned


def test_unofficial_full_clear_needs_consensus():
    agg = Aggregator()
    feed(agg, "mon1", "Київ: шахеди", 0)
    feed(agg, "mon1", "Відбій тривоги, Київ", 30)
    assert kyiv(agg) is not None          # Single source with weight 0.5 is insufficient
    feed(agg, "mon2", "Київ — відбій", 31)
    assert kyiv(agg) is None              # 0.75 >= 0.6 — all-clear accepted


def test_new_threat_cancels_pending_clear_votes():
    agg = Aggregator()
    feed(agg, "official", "Київ: шахеди", 0)
    feed(agg, "mon1", "Відбій тривоги, Київ", 10)
    feed(agg, "official", "Київ: нова група шахедів", 11)
    feed(agg, "mon2", "Київ — відбій", 12)
    assert kyiv(agg) is not None          # mon1 vote was invalidated by the new threat at minute 11


def test_edited_and_deleted_posts_replace_what_they_said():
    from dataclasses import replace

    agg = Aggregator()
    ev = extract(RawMessage(source="mon1", message_id=500, text="Київ — балістика!", date=T0))
    agg.add(ev)
    assert kyiv(agg).level is AlertLevel.RED

    # Edited into drones over Brovary: Kyiv's red is gone, Brovary gets yellow
    edited = extract(RawMessage(source="mon1", message_id=500, text="Бровари — шахеди", date=T0))
    changed = {s.district: s for s in agg.add(edited)}
    assert changed["kyiv_all"].level is AlertLevel.NONE
    assert changed["brovarskyi"].level is AlertLevel.YELLOW

    # Deleted: nothing it said remains
    gone = replace(edited, districts=[], raw_text="", retracted=True)
    assert [s.level for s in agg.add(gone)] == [AlertLevel.NONE]
    assert agg.snapshot() == []
