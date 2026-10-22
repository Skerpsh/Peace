"""Shared test fixtures: dummy environment variables and fixed channel list."""
import os

import pytest

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("MIN_CONFIDENCE", "0.6")
os.environ.setdefault("STALE_AFTER_MINUTES", "30")

TEST_CHANNELS = [
    {"username": "official", "weight": 1.0, "official": True},
    {"username": "mon1", "weight": 0.5, "official": False},
    {"username": "mon2", "weight": 0.5, "official": False},
]


@pytest.fixture(autouse=True)
def _fixed_channels(monkeypatch):
    # Decouple tests from real config/channels.yaml
    monkeypatch.setattr("peace.analyzer.scoring.channels", lambda: TEST_CHANNELS)
