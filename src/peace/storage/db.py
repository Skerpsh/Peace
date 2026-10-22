"""SQLite: подписчики и история событий (для отладки и улучшения парсера)."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from peace.models import ParsedEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   INTEGER NOT NULL,
    district  TEXT    NOT NULL,   -- '*' = все районы
    PRIMARY KEY (chat_id, district)
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT,
    message_id INTEGER,
    ts         TEXT,
    level      TEXT,
    districts  TEXT,
    threats    TEXT,
    direction  TEXT,
    raw_text   TEXT
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def subscribe(self, chat_id: int, district: str = "*") -> None:
        await self.conn.execute("INSERT OR IGNORE INTO subscribers VALUES (?, ?)", (chat_id, district))
        await self.conn.commit()

    async def unsubscribe(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def subscribers_for(self, district: str) -> list[int]:
        cur = await self.conn.execute(
            "SELECT DISTINCT chat_id FROM subscribers WHERE district IN (?, '*')", (district,)
        )
        return [r[0] for r in await cur.fetchall()]

    async def log_event(self, ev: ParsedEvent) -> None:
        await self.conn.execute(
            "INSERT INTO events (source, message_id, ts, level, districts, threats, direction, raw_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev.source, ev.message_id, ev.timestamp.isoformat(), ev.level.value,
             json.dumps(ev.districts), json.dumps(ev.threats), ev.direction, ev.raw_text),
        )
        await self.conn.commit()
