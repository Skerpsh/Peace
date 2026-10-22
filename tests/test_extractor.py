from datetime import datetime, timezone

from peace.models import AlertLevel, RawMessage
from peace.parser.extractor import extract


def _msg(text: str) -> RawMessage:
    return RawMessage(source="test", message_id=1, text=text, date=datetime.now(timezone.utc))


def test_shahed_is_yellow():
    ev = extract(_msg("Повітряна тривога! Група БпЛА на Броварщині курсом на Київ"))
    assert ev is not None
    assert ev.level is AlertLevel.YELLOW
    assert "shahed" in ev.threats
    assert "brovarskyi" in ev.districts
    assert ev.direction and ev.direction.startswith("київ")


def test_ballistic_is_red_and_removes_generic_missile():
    ev = extract(_msg("Загроза балістичної ракети для Києва"))
    assert ev is not None
    assert ev.level is AlertLevel.RED
    assert ev.threats == ["ballistic"]


def test_cruise_missile_is_red():
    ev = extract(_msg("Крилаті ракети в напрямку Київщини"))
    assert ev is not None and ev.level is AlertLevel.RED


def test_missile_and_drones_is_red_with_full_list():
    ev = extract(_msg("Київ: шахеди з півночі, також крилаті ракети з півдня"))
    assert ev is not None and ev.level is AlertLevel.RED
    assert set(ev.threats) == {"shahed", "cruise_missile"}


def test_threat_type_wins_over_emoji():
    # Even if a channel uses 🔴, Shahed drones map to yellow
    ev = extract(_msg("🔴 Київ — шахеди над Дніпром"))
    assert ev is not None and ev.level is AlertLevel.YELLOW


def test_alert_without_type():
    ev = extract(_msg("Повітряна тривога у м. Київ"))
    assert ev is not None and ev.level is AlertLevel.ALERT


def test_all_clear():
    ev = extract(_msg("🟢 Відбій тривоги у м. Київ"))
    assert ev is not None and ev.is_all_clear


def test_partial_clear_missiles_drones_remain():
    ev = extract(_msg("Київ: загроза ракет минула, БпЛА лишаються"))
    assert ev is not None
    assert not ev.is_all_clear
    assert ev.level is AlertLevel.YELLOW
    assert ev.threats == ["shahed"]
    assert {"missile", "ballistic", "cruise_missile"} <= set(ev.cleared_threats)


def test_partial_clear_only():
    ev = extract(_msg("🟢 Відбій ракетної небезпеки для Києва"))
    assert ev is not None
    assert not ev.is_all_clear
    assert ev.threats == []
    assert "cruise_missile" in ev.cleared_threats


def test_irrelevant_region_ignored():
    assert extract(_msg("🔴 Тривога у Харківській області")) is None
