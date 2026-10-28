"""Runs every case from tests/corpus.yaml through the extractor."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from peace.parser.extractor import extract
from peace.models import RawMessage

CASES = yaml.safe_load((Path(__file__).parent / "corpus.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["text"][:40] for c in CASES])
def test_corpus(case):
    ev = extract(RawMessage(source=case.get("source", "test"), message_id=1, text=case["text"],
                            date=datetime.now(timezone.utc)))
    if case.get("ignored"):
        assert ev is None
        return
    assert ev is not None
    if "districts" in case:
        assert sorted(ev.districts) == sorted(case["districts"])
    if "level" in case:
        assert ev.level.value == case["level"]
    if "threats" in case:
        assert sorted(ev.threats) == sorted(case["threats"])
    if "all_clear" in case:
        assert ev.is_all_clear == case["all_clear"]
