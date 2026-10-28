"""Admin monitoring: silent reports, a daily summary, and alarms with sound.

- Every MONITOR_REPORT_MINUTES (at :00, :15, ...), silently: subscribers, active zones,
  traffic since the previous report, sources, server (disk, RAM, load), the bot, the last backup.
- At midnight (Kyiv): the day compared with the day before.
- Every MONITOR_CHECK_SECONDS: problems that lasted two checks raise an alarm, and again
  when fixed: low disk or memory, the newest backup too old. (Failing sources: see health.py.)
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from peace.analyzer.aggregator import Aggregator
from peace.bot.formatter import KYIV_TZ
from peace.bot.notifier import Notifier
from peace.config import ROOT, channels, settings
from peace.health import Health
from peace.storage.db import Database

log = logging.getLogger(__name__)

ALERT_AFTER = 2                 # checks in a row before an alarm
DISK_MIN_FREE_PERCENT = 10
DISK_MIN_FREE_GB = 1
MEMORY_MIN_FREE_PERCENT = 5

STARTED = time.monotonic()


def version() -> str:
    try:
        return subprocess.run(["git", "describe", "--tags", "--always"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:  # noqa: BLE001
        return "?"


def server_metrics(path: str) -> dict:
    """Disk of `path`, RAM, load and this process's memory (Linux /proc)."""
    m: dict = {}
    du = shutil.disk_usage(path)
    m["disk_free_gb"] = du.free / 1024**3
    m["disk_free_pct"] = du.free * 100 / du.total
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total, avail = (int(info[k].split()[0]) for k in ("MemTotal", "MemAvailable"))
        m["mem_free_pct"] = avail * 100 / total
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
        m["rss_mb"] = int(status["VmRSS"].split()[0]) / 1024
        m["load"] = os.getloadavg()[0]
    except (OSError, KeyError, ValueError):
        pass
    return m


def backup_age_hours(backup_dir: str) -> float | None:
    """Age of the newest backup; None when there are none or the folder is not readable."""
    files = glob.glob(os.path.join(backup_dir, "peace-*.db.gz"))
    if not files:
        return None
    return (time.time() - max(os.path.getmtime(f) for f in files)) / 3600


def problems(metrics: dict, backup_age: float | None, max_backup_age: float) -> dict[str, str]:
    """Current problems, key -> text."""
    out = {}
    if metrics["disk_free_pct"] < DISK_MIN_FREE_PERCENT or metrics["disk_free_gb"] < DISK_MIN_FREE_GB:
        out["disk"] = f"Мало місця на диску: {metrics['disk_free_gb']:.1f} ГБ ({metrics['disk_free_pct']:.0f}%)"
    if metrics.get("mem_free_pct", 100) < MEMORY_MIN_FREE_PERCENT:
        out["memory"] = f"Мало пам'яті: вільно {metrics['mem_free_pct']:.0f}%"
    if backup_age is not None and backup_age > max_backup_age:
        out["backup"] = f"Останній бекап {backup_age:.0f} год тому"
    return out


class Alarms:
    """A problem alarms after ALERT_AFTER checks in a row, once, and reports when it is gone."""

    def __init__(self, after: int = ALERT_AFTER) -> None:
        self.after = after
        self.count: dict[str, int] = {}
        self.active: dict[str, str] = {}

    def update(self, current: dict[str, str]) -> list[str]:
        notices = []
        for key, text in current.items():
            self.count[key] = self.count.get(key, 0) + 1
            if self.count[key] >= self.after and key not in self.active:
                self.active[key] = text
                notices.append(f"🚨 {text}")
        for key in [k for k in self.count if k not in current]:
            del self.count[key]
            if key in self.active:
                notices.append(f"✅ Минуло: {self.active.pop(key)}")
        return notices


def _delta(now: int, before: int | None) -> str:
    if before is None or now == before:
        return ""
    return f" (▲{now - before})" if now > before else f" (▼{before - now})"


def _ago(hours: float | None) -> str:
    if hours is None:
        return "немає"
    return f"{hours * 60:.0f} хв тому" if hours < 1 else f"{hours:.0f} год тому"


def delay_text(delays, start: float, end: float = float("inf")) -> str:
    """Median and worst seconds from a source post to the last push of it, within [start, end)."""
    vals = sorted(d for t, d in delays if start <= t < end)
    if not vals:
        return "—"
    return f"медіана {vals[len(vals) // 2]:.0f} с, найгірша {vals[-1]:.0f} с"


def _uptime() -> str:
    mins = int((time.monotonic() - STARTED) // 60)
    return f"{mins // 1440} д {mins % 1440 // 60} год" if mins >= 1440 else f"{mins // 60} год {mins % 60} хв"


class Monitor:
    def __init__(self, bot, db: Database, agg: Aggregator, notifier: Notifier, health: Health) -> None:
        self.bot = bot
        self.db = db
        self.agg = agg
        self.notifier = notifier
        self.health = health
        self.alarms = Alarms()
        self.version = version()
        self._prev_subs: int | None = None
        self._prev_report = datetime.now(timezone.utc)
        self._prev_counters = dict(notifier.counters)
        self._day_push_base = 0   # pushes counter at the start of the day (or of the process)

    async def send(self, text: str, silent: bool = False) -> None:
        admin = settings().admin_chat_id
        if not admin:
            return
        try:
            await self.bot.send_message(admin, text, disable_notification=silent,
                                        disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Admin message failed: %s", e)

    # --- texts ------------------------------------------------------------

    async def report(self, advance: bool = True) -> str:
        """Traffic counts since the previous periodic report; `advance` starts a new period."""
        now = datetime.now(timezone.utc)
        since = self._prev_report
        cfg = settings()
        recipients = await self.db.recipients()
        subs = len(recipients)
        quiet = sum(r.quiet for r in recipients)
        events = await self.db.count_since("events", since.isoformat())
        ignored = await self.db.count_since("ignored", since.isoformat())
        c = self.notifier.counters
        pushes = c["push"] - self._prev_counters.get("push", 0)
        failed = c["failed"] - self._prev_counters.get("failed", 0)
        down = sorted(self.health.down)
        n_sources = len(channels()) + (1 if cfg.alerts_api_token else 0)
        m = server_metrics(str(Path(self.db.path).resolve().parent))
        mins = max(1, round((now - since).total_seconds() / 60))

        lines = [
            f"📊 <b>PEACE</b> · {now.astimezone(KYIV_TZ):%H:%M} · {self.version}",
            f"👥 Підписники: {subs}{_delta(subs, self._prev_subs)} · 🌙 {quiet}",
            f"🚨 Активні зони: {len(self.agg.snapshot())}",
            f"📨 За {mins} хв: подій {events}, нерозпізнаних {ignored}, пушів {pushes}"
            + (f", помилок {failed}" if failed else ""),
            f"⏱ Затримка (пост → пуш): {delay_text(self.notifier.delays, since.timestamp())}",
            f"📡 Джерела: {n_sources - len(down)}/{n_sources} працюють"
            + (f" · ⚠️ {', '.join(down)}" if down else ""),
            f"💾 Диск: {m['disk_free_gb']:.1f} ГБ вільно ({m['disk_free_pct']:.0f}%)"
            + (f" · RAM вільно {m['mem_free_pct']:.0f}% · load {m['load']:.2f}" if "mem_free_pct" in m else ""),
            f"🐍 Бот: {m.get('rss_mb', 0):.0f} МБ, працює {_uptime()}",
            f"🗄 Бекап: {_ago(backup_age_hours(cfg.backup_dir))}",
        ]
        if advance:
            self._prev_subs = subs
            self._prev_report = now
            self._prev_counters = dict(c)
        return "\n".join(lines)

    async def day_totals(self, day: datetime) -> dict:
        """Totals of the Kyiv calendar day that contains `day`."""
        start = day.astimezone(KYIV_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        s, e = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
        return {
            "delay": delay_text(self.notifier.delays, start.timestamp(), end.timestamp()),
            "subscribers": len(await self.db.recipients()),
            "events": await self.db.count_since("events", s, e),
            "ignored": await self.db.count_since("ignored", s, e),
            # shortcut: pushes are counted in memory, so a restart during the day loses the earlier ones
            "pushes": self.notifier.counters["push"] - self._day_push_base,
        }

    async def daily_summary(self, day: datetime, final: bool = True) -> str:
        """The day compared with the one before; `final` stores it for tomorrow's comparison."""
        today = await self.day_totals(day)
        pushes = today["pushes"]
        key = day.astimezone(KYIV_TZ).date()
        prev = await self.db.daily((key - timedelta(days=1)).isoformat()) or {}
        if final:
            await self.db.save_daily(key.isoformat(), today)
            self._day_push_base = self.notifier.counters["push"]
        title = "Підсумок" if final else "Сьогодні"
        return "\n".join([
            f"📅 <b>{title} {key:%d.%m}</b> (порівняно з попереднім днем)",
            f"👥 Підписники: {today['subscribers']}{_delta(today['subscribers'], prev.get('subscribers'))}",
            f"📨 Подій: {today['events']}{_delta(today['events'], prev.get('events'))}",
            f"❔ Нерозпізнаних: {today['ignored']}{_delta(today['ignored'], prev.get('ignored'))}",
            f"🔔 Пушів: {pushes}{_delta(pushes, prev.get('pushes'))}",
            f"⏱ Затримка: {today['delay']}",
        ])

    def health_text(self) -> str:
        lines = ["🩺 <b>Стан</b>"]
        for text in self.alarms.active.values():
            lines.append(f"🚨 {text}")
        for src in sorted(self.health.down):
            lines.append(f"⚠️ Джерело {src} не відповідає: {self.health.last_error.get(src, '')}")
        if len(lines) == 1:
            lines.append("✅ Проблем немає")
        return "\n".join(lines)

    # --- loop -------------------------------------------------------------

    async def run(self) -> None:
        cfg = settings()
        if not cfg.admin_chat_id:
            log.info("ADMIN_CHAT_ID is not set — admin monitoring is off")
            return
        while not self.notifier.ready:   # report the state after the startup preload
            await asyncio.sleep(1)
        await self.send(f"🚀 <b>PEACE запущено</b> · {self.version}\n\n" + await self.report())
        last_slot = None
        last_day = datetime.now(KYIV_TZ).date()
        while True:
            await asyncio.sleep(cfg.monitor_check_seconds)
            now = datetime.now(KYIV_TZ)
            try:
                m = server_metrics(str(Path(self.db.path).resolve().parent))
                for text in self.alarms.update(problems(m, backup_age_hours(cfg.backup_dir),
                                                        cfg.backup_max_age_hours)):
                    await self.send(text)
                if now.date() != last_day:
                    await self.send(await self.daily_summary(now - timedelta(days=1)), silent=True)
                    last_day = now.date()
                slot = (now.hour, now.minute // cfg.monitor_report_minutes)
                if now.minute % cfg.monitor_report_minutes == 0 and slot != last_slot:
                    await self.send(await self.report(), silent=True)
                    last_slot = slot
            except Exception:  # noqa: BLE001
                log.exception("Monitor check failed")

