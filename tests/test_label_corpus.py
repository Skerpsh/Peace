"""Comparison logic of scripts/label_corpus.py (no API calls)."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("anthropic")
_spec = importlib.util.spec_from_file_location(
    "label_corpus", Path(__file__).parents[1] / "scripts" / "label_corpus.py")
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)


def _label(**kw):
    return {"ignored": False, "districts": [], "level": "none", "threats": [], "all_clear": False} | kw


def test_agreement():
    exp = _label(districts=["kyiv_all"], level="yellow", threats=["shahed"])
    assert lc.diff(exp, lc.rules_label("Повітряна тривога на Троєщині, шахеди", "test")) == []


def test_false_alarm_and_miss():
    assert lc.diff({"ignored": True}, {"districts": ["kyiv_all"], "level": "red"}) == ["ignored"]
    assert lc.diff(_label(level="red"), {"ignored": True}) == ["ignored"]
    assert lc.diff({"ignored": True}, {"ignored": True}) == []


def test_wrong_fields():
    got = {"districts": ["kyiv_all"], "level": "yellow", "threats": ["shahed"], "all_clear": False}
    exp = _label(districts=["kyiv_all"], level="red", threats=["ballistic"])
    assert lc.diff(exp, got) == ["level", "threats"]


def test_schema_and_prompt_build():
    schema = lc._schema()
    assert "kyiv_all" in schema["properties"]["districts"]["items"]["enum"]
    assert "shahed" in lc._system()
