"""Process-wide runtime state. `db` is created inside the running event loop (see main.on_startup)
so motor binds to the right loop."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from .config import SETTINGS

if TYPE_CHECKING:
    from .database import Database


class AppState:
    def __init__(self) -> None:
        self.db: "Database" = None  # type: ignore[assignment]
        self.posting_locks: dict[int, asyncio.Lock] = {}
        self.bot_username: str = ""
        self.started_monotonic = time.monotonic()
        self.title_cache: dict[int, str | None] = {}
        self.media_group_buffers: dict[str, dict[str, Any]] = {}

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self.posting_locks.get(chat_id)
        if lock is None:
            lock = self.posting_locks[chat_id] = asyncio.Lock()
        return lock

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_monotonic)


STATE = AppState()


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in SETTINGS.admin_ids)
