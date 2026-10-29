"""Scoring logic of scripts/source_weights.py."""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "source_weights", Path(__file__).parents[1] / "scripts" / "source_weights.py")
sw = importlib.util.module_from_spec(_spec)
sys.modules["source_weights"] = sw   # dataclasses look their module up while being created
_spec.loader.exec_module(sw)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _a(src, minute, text, district="kyiv_all", level="yellow"):
    return sw.Alert(src, T0 + timedelta(minutes=minute), district, level, text)


def test_confirmation_and_lead():
    scores = sw.score([
        _a("fast", 0, "шахеди на київ"),
        _a("slow", 3, "київ — дрони"),
        _a("fast", 60, "дрони над києвом"),            # nobody confirmed
        _a("slow", 90, "балістика", level="red"),     # other level: no confirmation
    ])
    assert scores["fast"].confirmed == 0.5 and scores["fast"].lead_min == 3
    assert scores["slow"].confirmed == 0.5 and scores["slow"].lead_min == -3


def test_reposts_do_not_confirm_and_small_samples_get_no_weight():
    scores = sw.score([_a("a", 0, "шахеди"), _a("b", 1, "шахеди")])  # same text = one post
    assert scores["a"].confirmed == 0
    assert sw.suggest(scores["a"]) is None
    assert sw.suggest(sw.Score(50, 0.97, 1.0)) == 0.9
    assert sw.suggest(sw.Score(50, 0.1, 1.0)) == 0.3
