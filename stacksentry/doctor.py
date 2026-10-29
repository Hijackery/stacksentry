"""`stacksentry-doctor`: быстрая самодиагностика установки."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from . import __version__
from .monitor import Monitor
from .settings import ConfigError, Settings, load_settings
from .storage import Storage, StorageError

_OK = "✅"
_WARN = "⚠️"
_FAIL = "❌"


def _service(name: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "неизвестно"
    return proc.stdout.strip() or "неизвестно"


def _line(mark: str, title: str, detail: str = "") -> None:
    print(f"{mark} {title}" + (f": {detail}" if detail else ""))


async def _panel_check(settings: Settings) -> bool:
    monitor = Monitor(settings)
    try:
        snapshot = await monitor.snapshot()
    finally:
        await monitor.close()
    if snapshot.panel.disabled:
        _line(_OK, "Панель", "monitor-only")
    elif snapshot.panel.reachable:
        _line(_OK, "Панель", f"отвечает, {snapshot.panel.latency_ms or 0:.0f} мс")
    else:
        _line(_FAIL, "Панель", snapshot.panel.error or "нет ответа")
        return False
    down = [item.name for item in snapshot.services if not item.active]
    if down:
        _line(_WARN, "Службы", "не работают: " + ", ".join(down))
    else:
        _line(_OK, "Службы", "все активны")
    for warning in snapshot.warnings[:5]:
        _line(_WARN, "Замечание", warning)
    return not down and snapshot.panel.reachable


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    no_network = "--no-network" in args
    failures = 0
    print(f"StackSentry doctor {__version__}")
    try:
        settings = load_settings()
    except ConfigError as exc:
        _line(_FAIL, "Конфигурация", str(exc))
        return 1
    _line(_OK, "Конфигурация", "загружена")

    try:
        storage = Storage(settings.state_path)
    except StorageError as exc:
        _line(_FAIL, "База", str(exc))
        return 1
    try:
        samples = storage.count_samples()
        pending = storage.count_pending_alerts()
        _line(_OK, "База", f"{samples} сэмплов, {pending} алертов в outbox")
    finally:
        storage.close()

    if os.access(settings.log_path.parent, os.W_OK):
        _line(_OK, "Каталог логов", str(settings.log_path.parent))
    else:
        _line(_WARN, "Каталог логов", f"нет записи: {settings.log_path.parent}")

    service = os.getenv("SERVICE_NAME", "stacksentry-bot")
    state = _service(service)
    if state == "active":
        _line(_OK, "Служба бота", service)
    else:
        _line(_FAIL, "Служба бота", f"{service}: {state}")
        failures += 1

    if settings.webapp_enabled:
        webapp = os.getenv("WEBAPP_SERVICE_NAME", "stacksentry-webapp")
        state = _service(webapp)
        if state == "active":
            _line(_OK, "Mini App", webapp)
        else:
            _line(_WARN, "Mini App", f"{webapp}: {state}")

    if not no_network and settings.panel_enabled:
        try:
            if not asyncio.run(_panel_check(settings)):
                failures += 1
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
            _line(_FAIL, "Панель", str(exc) or exc.__class__.__name__)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
