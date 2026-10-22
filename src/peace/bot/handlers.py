"""Команды бота для пользователей (aiogram 3)."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import format_snapshot
from peace.config import regions
from peace.storage.db import Database


def build_router(db: Database, aggregator: Aggregator) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(m: Message) -> None:
        await db.subscribe(m.chat.id, "*")
        await m.answer(
            "Привіт! Я стежу за повітряними тривогами по Києву та області.\n\n"
            "/status — поточна ситуація\n"
            "/districts — список районів\n"
            "/subscribe &lt;район&gt; — тільки обраний район\n"
            "/stop — відписатися"
        )

    @router.message(Command("status"))
    async def status(m: Message) -> None:
        await m.answer(format_snapshot(aggregator.snapshot()))

    @router.message(Command("districts"))
    async def districts(m: Message) -> None:
        lines = [f"<code>{k}</code> — {v['name']}" for k, v in regions().items()]
        await m.answer("\n".join(lines))

    @router.message(Command("subscribe"))
    async def subscribe(m: Message, command: CommandObject) -> None:
        key = (command.args or "").strip()
        if key not in regions():
            await m.answer("Вкажіть район з /districts, напр. <code>/subscribe obolonskyi</code>")
            return
        await db.unsubscribe(m.chat.id)
        await db.subscribe(m.chat.id, key)
        await m.answer(f"Готово, підписка на {regions()[key]['name']}")

    @router.message(Command("stop"))
    async def stop(m: Message) -> None:
        await db.unsubscribe(m.chat.id)
        await m.answer("Ви відписані. /start — підписатися знову.")

    return router
