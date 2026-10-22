"""Configuration loader for .env and YAML files."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    tg_api_id: int
    tg_api_hash: str
    tg_session_name: str = "data/peace_userbot"
    bot_token: str
    alerts_api_token: str = ""
    db_path: str = "data/peace.db"
    # Threats do not expire; after this many minutes without updates, a staleness notice is shown
    stale_after_minutes: int = 30
    min_confidence: float = 0.6


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@lru_cache
def channels() -> list[dict]:
    return _load_yaml("channels.yaml")["channels"]


@lru_cache
def regions() -> dict[str, dict]:
    """Flat dictionary: key -> {name, aliases, area}."""
    data = _load_yaml("regions.yaml")
    flat: dict[str, dict] = {}
    for area, items in data.items():
        for key, info in items.items():
            flat[key] = {**info, "area": area}
    return flat


@lru_cache
def threats() -> dict:
    return _load_yaml("threats.yaml")
