"""Общая база миксинов бота: атрибуты, которые задаёт MonitorBot."""

from __future__ import annotations

import asyncio
from typing import Any

from .actions import ActionManager
from .alerts import AlertEngine
from .inventory import PlatformInfo
from .models import MonitorSnapshot
from .monitor import Monitor
from .settings import Settings
from .storage import Storage


class BotBase:
    """Объявляет состояние бота, доступное методам миксинов.

    Сами значения создаются в ``MonitorBot.__init__``. Методы, оставшиеся в
    ``MonitorBot``, mypy находит через ``__getattr__``: это позволяет не
    дублировать их сигнатуры в базе.
    """

    settings: Settings
    storage: Storage
    monitor: Monitor
    engine: AlertEngine
    actions: ActionManager
    platform: PlatformInfo
    app: Any
    _last_screen: dict[int, int]
    _screen_locks: dict[int, asyncio.Lock]
    _log_searches: dict[int, str]
    _snapshot: MonitorSnapshot | None
    _snapshot_at: float

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)
