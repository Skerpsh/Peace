"""Source health tracking: collectors report successes/failures, the watcher alerts the admin.

Channel silence is normal (no alerts), so only fetch failures count, not missing posts.
"""
from __future__ import annotations

import asyncio
from html import escape
import time
from collections.abc import Awaitable, Callable

DOWN_AFTER_SEC = 10 * 60
CHECK_EVERY_SEC = 60


class Health:
    def __init__(self) -> None:
        self.failing_since: dict[str, float] = {}
        self.last_error: dict[str, str] = {}
        self.down: set[str] = set()

    def ok(self, source: str) -> None:
        self.failing_since.pop(source, None)

    def fail(self, source: str, error: object) -> None:
        self.failing_since.setdefault(source, time.monotonic())
        self.last_error[source] = escape(str(error)[:200])

    def check(self, now: float | None = None) -> list[str]:
        """Returns notices for sources that just went down or recovered."""
        now = now if now is not None else time.monotonic()
        notices = []
        for src, since in self.failing_since.items():
            if src not in self.down and now - since >= DOWN_AFTER_SEC:
                self.down.add(src)
                mins = int((now - since) // 60)
                notices.append(f"⚠️ Джерело <b>{src}</b> не відповідає {mins} хв: {self.last_error.get(src, '')}")
        for src in [s for s in self.down if s not in self.failing_since]:
            self.down.discard(src)
            notices.append(f"✅ Джерело <b>{src}</b> знову працює")
        return notices

    async def watch(self, notify: Callable[[str], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY_SEC)
            for text in self.check():
                await notify(text)


health = Health()
