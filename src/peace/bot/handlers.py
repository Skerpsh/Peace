"""Telegram bot command handlers (aiogram 3)."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from peace.bot.formatter import LEVEL_ICON, district_name
from peace.bot.notifier import Notifier
from peace.config import regions, settings
from peace.models import RawMessage
from peace.monitor import Monitor
from peace.parser.extractor import extract
from peace.storage.db import Database

BTN_STATUS = "📍 Статус"
BTN_ZONES = "🗺 Мої зони"
BTN_QUIET = "🌙 Нічний режим"

COMMANDS = [
    BotCommand(command="status", description="Поточна ситуація"),
    BotCommand(command="zones", description="Обрати зони для сповіщень"),
    BotCommand(command="quiet", description="Нічний режим: лише 🔴 та відбій"),
    BotCommand(command="stop", description="Відписатися від сповіщень"),
    BotCommand(command="help", description="Довідка"),
]

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_ZONES)],
              [KeyboardButton(text=BTN_QUIET)]],
    resize_keyboard=True,
    is_persistent=True,
)

HELP = (
    "Я стежу за повітряними тривогами по <b>Києву</b> та <b>Київській області</b> "
    "і звіряю кілька джерел між собою.\n\n"
    "🔴 ракетна небезпека · 🟡 загроза БпЛА · ⚠️ тривога, тип уточнюється · 🟢 відбій\n\n"
    "Закріплене повідомлення «Статус зараз» оновлюється само. "
    "Окремі сповіщення приходять, коли загроза посилюється або настає відбій.\n\n"
    "/status — поточна ситуація\n"
    "/zones — обрати зони\n"
    "/quiet — нічний режим\n"
    "/stop — відписатися"
)


def toggle_zone(zones: set[str], key: str, all_keys: list[str]) -> set[str]:
    """'*' means every zone; picking a single zone out of '*' keeps all the others."""
    if key == "*":
        return set() if "*" in zones else {"*"}
    if "*" in zones:
        return set(all_keys) - {key}
    zones = zones ^ {key}
    return {"*"} if zones >= set(all_keys) else zones


def _zones_keyboard(zones: set[str]) -> InlineKeyboardMarkup:
    everything = "*" in zones

    def mark(on: bool) -> str:
        return "✅" if on else "▫️"

    rows = [[InlineKeyboardButton(text=f"{mark(everything)} Усі зони", callback_data="z:*")]]
    for key in regions():
        rows.append([InlineKeyboardButton(
            text=f"{mark(everything or key in zones)} {district_name(key)}", callback_data=f"z:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _zones_text(zones: set[str]) -> str:
    if not zones:
        return "🗺 <b>Зони сповіщень</b>\n\nЗараз ви не отримуєте сповіщень. Оберіть хоча б одну зону:"
    return "🗺 <b>Зони сповіщень</b>\n\nНатисніть, щоб увімкнути або вимкнути зону:"


def build_router(db: Database, notifier: Notifier, monitor: Monitor) -> Router:
    router = Router()
    admin_id = settings().admin_chat_id

    @router.message(CommandStart())
    async def start(m: Message) -> None:
        if not await db.zones(m.chat.id):
            await db.subscribe(m.chat.id, "*")
        await m.answer("👋 Вітаю!\n\n" + HELP, reply_markup=KEYBOARD)
        await notifier.send_live(m.chat.id, await db.zones(m.chat.id))

    @router.message(Command("help"))
    async def help_(m: Message) -> None:
        await m.answer(HELP, reply_markup=KEYBOARD)

    @router.message(Command("status"))
    @router.message(F.text == BTN_STATUS)
    async def status(m: Message) -> None:
        await notifier.send_live(m.chat.id, await db.zones(m.chat.id))

    @router.message(Command("zones", "districts", "subscribe"))
    @router.message(F.text == BTN_ZONES)
    async def zones(m: Message) -> None:
        current = await db.zones(m.chat.id)
        await m.answer(_zones_text(current), reply_markup=_zones_keyboard(current))

    @router.callback_query(F.data.startswith("z:"))
    async def zone_toggle(q: CallbackQuery) -> None:
        key = q.data[2:]
        if key != "*" and key not in regions():
            await q.answer()
            return
        chat_id = q.message.chat.id
        new = toggle_zone(await db.zones(chat_id), key, list(regions()))
        await db.set_zones(chat_id, new)
        await q.message.edit_text(_zones_text(new), reply_markup=_zones_keyboard(new))
        await q.answer("Збережено")

    @router.message(Command("quiet"))
    @router.message(F.text == BTN_QUIET)
    async def quiet(m: Message) -> None:
        on = not await db.quiet(m.chat.id)
        await db.set_quiet(m.chat.id, on)
        cfg = settings()
        hours = f"{cfg.quiet_start_hour:02d}:00–{cfg.quiet_end_hour:02d}:00"
        if on:
            await m.answer(f"🌙 Нічний режим увімкнено. З {hours} сповіщення приходитимуть "
                           "лише про 🔴 ракетну небезпеку та відбій. Статус оновлюється як зазвичай.")
        else:
            await m.answer("☀️ Нічний режим вимкнено — сповіщення приходять цілодобово.")

    @router.message(Command("stop"))
    async def stop(m: Message) -> None:
        await db.unsubscribe(m.chat.id)
        await m.answer("Ви відписалися від сповіщень. Щоб повернутися — /start або /zones.")

    @router.message(Command("id"))
    async def chat_id(m: Message) -> None:
        await m.answer(f"Ваш chat id: <code>{m.chat.id}</code>")

    def is_admin(m: Message) -> bool:
        return bool(admin_id) and m.chat.id == admin_id

    @router.message(Command("stats"), is_admin)
    async def stats(m: Message) -> None:
        await m.answer(await monitor.report(advance=False))

    @router.message(Command("today"), is_admin)
    async def today(m: Message) -> None:
        await m.answer(await monitor.daily_summary(datetime.now(timezone.utc), final=False))

    @router.message(Command("health"), is_admin)
    async def health_(m: Message) -> None:
        await m.answer(monitor.health_text())

    @router.message(Command("debug"))
    async def debug(m: Message, command: CommandObject) -> None:
        if not is_admin(m):
            return
        if command.args:
            ev = extract(RawMessage(source="debug", message_id=0, text=command.args,
                                    date=datetime.now(timezone.utc)))
            if not ev:
                await m.answer("❌ Не розпізнано (повідомлення буде проігноровано)")
                return
            await m.answer(
                f"{LEVEL_ICON[ev.level]} рівень: <b>{ev.level.value}</b>\n"
                f"зони: {', '.join(ev.districts)}\n"
                f"загрози: {', '.join(ev.threats) or '—'}\n"
                f"знято: {', '.join(ev.cleared_threats) or '—'}\n"
                f"відбій: {'так' if ev.is_all_clear else 'ні'}\n"
                f"напрямок: {escape(ev.direction or '—')}"
            )
            return
        rows = await db.recent_ignored(10)
        if not rows:
            await m.answer("Нерозпізнаних повідомлень немає.")
            return
        parts = [f"<b>{escape(src)}#{mid}</b> {ts[11:16]}\n{escape(text[:300])}" for src, mid, ts, text in rows]
        await m.answer("🔍 <b>Останні нерозпізнані повідомлення</b>\n\n" + "\n\n".join(parts),
                       disable_web_page_preview=True)

    @router.message()
    async def fallback(m: Message) -> None:
        await m.answer(HELP, reply_markup=KEYBOARD)

    return router
