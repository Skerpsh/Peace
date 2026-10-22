from peace.collector.official_api import parse_active_alerts
from peace.models import AlertLevel


def test_official_api_parses_yellow_drones_and_raion():
    payload = {
        "alerts": [
            {
                "location_title": "Броварський район",
                "location_type": "raion",
                "alert_type": "air_raid",
                "location_uid": "101",
                "location_oblast": "Київська область",
                "location_oblast_uid": "14",
                "location_raion": "Броварський район",
                "alert_level": "yellow",
                "threats": [{"threat_type": "drones", "level": "yellow"}],
            }
        ]
    }
    events, state = parse_active_alerts(payload, {})
    assert len(events) == 1
    ev = events[0]
    assert ev.districts == ["brovarskyi"]
    assert ev.level is AlertLevel.YELLOW
    assert ev.threats == ["shahed"]
    assert not ev.is_all_clear


def test_official_api_partial_clear_and_all_clear():
    payload_red = {
        "alerts": [
            {
                "location_title": "м. Київ",
                "alert_type": "air_raid",
                "location_uid": "31",
                "alert_level": "red",
                "threats": [
                    {"threat_type": "cruise_missiles", "level": "red"},
                    {"threat_type": "drones", "level": "yellow"},
                ],
            }
        ]
    }
    events, state = parse_active_alerts(payload_red, {})
    assert events[0].level is AlertLevel.RED
    assert set(events[0].threats) == {"cruise_missile", "shahed"}

    # Missiles cleared, drones remain
    payload_yellow = {
        "alerts": [
            {
                "location_title": "м. Київ",
                "alert_type": "air_raid",
                "location_uid": "31",
                "alert_level": "yellow",
                "threats": [{"threat_type": "drones", "level": "yellow"}],
            }
        ]
    }
    events, state = parse_active_alerts(payload_yellow, state)
    assert len(events) == 1
    assert events[0].level is AlertLevel.YELLOW
    assert events[0].threats == ["shahed"]
    assert events[0].cleared_threats == ["cruise_missile"]

    # Full all-clear
    events, state = parse_active_alerts({"alerts": []}, state)
    assert len(events) == 1
    assert events[0].districts == ["kyiv_all"]
    assert events[0].is_all_clear
