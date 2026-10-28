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

    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_name: str = "data/peace_userbot"
    bot_token: str
    alerts_api_token: str = ""
    db_path: str = "data/peace.db"
    # Threats do not expire early; after this many minutes without updates, a staleness notice is shown
    stale_after_minutes: int = 30
    min_confidence: float = 0.6
    # Telegram chat id that receives source-failure notices and may use /debug (0 = disabled)
    admin_chat_id: int = 0
    # Night mode window (Kyiv time, hours): only red alerts and all-clears trigger a push
    quiet_start_hour: int = 23
    quiet_end_hour: int = 7
    # Changes arriving within this window are merged into one notification
    batch_seconds: float = 4.0
    # Admin monitoring: a silent report every N minutes, problem checks every N seconds
    monitor_report_minutes: int = 15
    monitor_check_seconds: int = 60
    backup_dir: str = "/var/backups/peace"
    backup_max_age_hours: int = 26


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
