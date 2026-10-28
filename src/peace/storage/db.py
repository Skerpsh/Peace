"""SQLite storage for subscribers, user settings, and parsed/ignored message history."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from peace.models import ParsedEvent, RawMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   INTEGER NOT NULL,
    district  TEXT    NOT NULL,   -- '*' = all districts
    PRIMARY KEY (chat_id, district)
);
CREATE TABLE IF NOT EXISTS users (
    chat_id       INTEGER PRIMARY KEY,
    quiet         INTEGER NOT NULL DEFAULT 0,   -- night mode: push only red and all-clear
    status_msg_id INTEGER                       -- live "status now" message edited in place
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
CREATE TABLE IF NOT EXISTS ignored (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT,
    message_id INTEGER,
    ts         TEXT,
    text       TEXT
);
"""

# Kyiv used to be split into city districts; it is now a single kyiv_all zone.
_OLD_KYIV_KEYS = (
    "holosiivskyi", "darnytskyi", "desnianskyi", "dniprovskyi", "obolonskyi", "pecherskyi",
    "podilskyi", "sviatoshynskyi", "solomianskyi", "shevchenkivskyi",
)
_OLD_KEYS_SQL = ",".join("?" * len(_OLD_KYIV_KEYS))


@dataclass(slots=True)
class Recipient:
    chat_id: int
    zones: set[str] = field(default_factory=set)   # may contain '*'
    quiet: bool = False
    status_msg_id: int | None = None

    def wants(self, district: str) -> bool:
        return "*" in self.zones or district in self.zones


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.executescript(SCHEMA)
        await self.conn.execute(
            "INSERT OR IGNORE INTO subscribers SELECT chat_id, 'kyiv_all' FROM subscribers"
            f" WHERE district IN ({_OLD_KEYS_SQL})", _OLD_KYIV_KEYS,
        )
        await self.conn.execute(f"DELETE FROM subscribers WHERE district IN ({_OLD_KEYS_SQL})", _OLD_KYIV_KEYS)
        await self.conn.commit()

    # --- subscriptions ----------------------------------------------------

    async def subscribe(self, chat_id: int, district: str = "*") -> None:
        await self.conn.execute("INSERT OR IGNORE INTO subscribers VALUES (?, ?)", (chat_id, district))
        await self.conn.commit()

    async def unsubscribe(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def zones(self, chat_id: int) -> set[str]:
        cur = await self.conn.execute("SELECT district FROM subscribers WHERE chat_id = ?", (chat_id,))
        return {r[0] for r in await cur.fetchall()}

    async def set_zones(self, chat_id: int, zones: set[str]) -> None:
        await self.conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await self.conn.executemany("INSERT INTO subscribers VALUES (?, ?)", [(chat_id, z) for z in zones])
        await self.conn.commit()

    async def subscribers_for(self, district: str) -> list[int]:
        cur = await self.conn.execute(
            "SELECT DISTINCT chat_id FROM subscribers WHERE district IN (?, '*')", (district,)
        )
        return [r[0] for r in await cur.fetchall()]

    async def recipients(self) -> list[Recipient]:
        """All subscribed chats with their zones and settings, in one query."""
        cur = await self.conn.execute(
            "SELECT s.chat_id, s.district, COALESCE(u.quiet, 0), u.status_msg_id"
            " FROM subscribers s LEFT JOIN users u ON u.chat_id = s.chat_id"
        )
        out: dict[int, Recipient] = {}
        for chat_id, district, quiet, msg_id in await cur.fetchall():
            r = out.setdefault(chat_id, Recipient(chat_id, quiet=bool(quiet), status_msg_id=msg_id))
            r.zones.add(district)
        return list(out.values())

    async def forget(self, chat_id: int) -> None:
        """Removes a chat entirely (e.g. the user blocked the bot)."""
        await self.conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await self.conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    # --- user settings ----------------------------------------------------

    async def quiet(self, chat_id: int) -> bool:
        cur = await self.conn.execute("SELECT quiet FROM users WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        return bool(row and row[0])

    async def set_quiet(self, chat_id: int, quiet: bool) -> None:
        await self.conn.execute(
            "INSERT INTO users (chat_id, quiet) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET quiet = excluded.quiet",
            (chat_id, int(quiet)),
        )
        await self.conn.commit()

    async def set_status_msg(self, chat_id: int, msg_id: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO users (chat_id, status_msg_id) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET status_msg_id = excluded.status_msg_id",
            (chat_id, msg_id),
        )
        await self.conn.commit()

    # --- history ----------------------------------------------------------

    async def log_event(self, ev: ParsedEvent) -> None:
        await self.conn.execute(
            "INSERT INTO events (source, message_id, ts, level, districts, threats, direction, raw_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev.source, ev.message_id, ev.timestamp.isoformat(), ev.level.value,
             json.dumps(ev.districts), json.dumps(ev.threats), ev.direction, ev.raw_text),
        )
        await self.conn.commit()

    async def log_ignored(self, msg: RawMessage) -> None:
        """Keeps messages the parser did not recognise, to improve rules from real data."""
        await self.conn.execute(
            "INSERT INTO ignored (source, message_id, ts, text) VALUES (?, ?, ?, ?)",
            (msg.source, msg.message_id, msg.date.isoformat(), msg.text),
        )
        await self.conn.commit()

    async def recent_ignored(self, limit: int = 10) -> list[tuple[str, int, str, str]]:
        cur = await self.conn.execute(
            "SELECT source, message_id, ts, text FROM ignored ORDER BY id DESC LIMIT ?", (limit,)
        )
        return list(await cur.fetchall())
