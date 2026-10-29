"""Константы бота StackSentry."""

from __future__ import annotations

from .actions import (
    ACTION_APT_UPDATE,
    ACTION_COMMAND,
    ACTION_DIST_UPGRADE,
    ACTION_JOURNAL,
    ACTION_REBOOT,
    ACTION_RELEASE_UPGRADE,
    ACTION_RESTART_SERVICE,
    ACTION_RESTART_XRAY,
    ACTION_UPDATE_BOT,
    ACTION_UPGRADE,
    ACTION_UPGRADE_PHASED,
)

__all__ = [
    "ACTION_APT_UPDATE",
    "ACTION_COMMAND",
    "ACTION_DIST_UPGRADE",
    "ACTION_JOURNAL",
    "ACTION_REBOOT",
    "ACTION_RELEASE_UPGRADE",
    "ACTION_RESTART_SERVICE",
    "ACTION_RESTART_XRAY",
    "ACTION_RUNNING",
    "ACTION_TITLES",
    "ACTION_UPDATE_BOT",
    "ACTION_UPGRADE",
    "ACTION_UPGRADE_PHASED",
    "APT_WATCH_ERROR_LIMIT",
    "APT_WATCH_MAX",
    "APT_WATCH_STEP",
    "CONFIRM_DESCRIPTIONS",
    "CRITICAL_ACTIONS",
    "CRITICAL_FRESH_WINDOW",
    "HEAVY_COOLDOWN",
    "MUTE_HOURS",
    "PROTECTED_ACTIONS",
    "QUEUED_ACTIONS",
    "REBOOT_WATCH_ATTEMPTS",
    "REBOOT_WATCH_GRACE",
    "REBOOT_WATCH_STEP",
    "REFRESH_COOLDOWN",
    "SAMPLE_RETENTION_DAYS",
    "SNAPSHOT_MAX_AGE",
    "TWOFA_PROMPT_TTL",
]

SNAPSHOT_MAX_AGE = 30.0

SAMPLE_RETENTION_DAYS = 7

REBOOT_WATCH_ATTEMPTS = 20

REBOOT_WATCH_STEP = 30

HEAVY_COOLDOWN = 5.0

REFRESH_COOLDOWN = 2.0

MUTE_HOURS = 8

REBOOT_WATCH_GRACE = 1200

APT_WATCH_STEP = 10

APT_WATCH_MAX = 2 * 3600

APT_WATCH_ERROR_LIMIT = 6

CRITICAL_ACTIONS = frozenset(
    {
        ACTION_UPGRADE,
        ACTION_UPGRADE_PHASED,
        ACTION_DIST_UPGRADE,
        ACTION_RELEASE_UPGRADE,
        ACTION_REBOOT,
        ACTION_UPDATE_BOT,
    }
)

PROTECTED_ACTIONS = CRITICAL_ACTIONS | {
    ACTION_RESTART_SERVICE,
    ACTION_RESTART_XRAY,
    ACTION_APT_UPDATE,
    ACTION_COMMAND,
    ACTION_UPDATE_BOT,
}

CRITICAL_FRESH_WINDOW = 120

TWOFA_PROMPT_TTL = 120

QUEUED_ACTIONS = (
    ACTION_APT_UPDATE,
    ACTION_UPGRADE,
    ACTION_UPGRADE_PHASED,
    ACTION_RELEASE_UPGRADE,
)

ACTION_TITLES: dict[str, str] = {
    ACTION_RESTART_SERVICE: "Сервис перезапущен",
    ACTION_RESTART_XRAY: "xray перезапущен",
    ACTION_APT_UPDATE: "Список пакетов обновлён",
    ACTION_UPGRADE: "Обновление системы завершено",
    ACTION_UPGRADE_PHASED: "Обновление (phased) завершено",
    ACTION_DIST_UPGRADE: "Проверка релиз-обновления выполнена",
    ACTION_RELEASE_UPGRADE: "Релиз-обновление завершено",
    ACTION_REBOOT: "Сервер перезагружается",
    ACTION_COMMAND: "Команда выполнена",
    ACTION_JOURNAL: "Журнал прочитан",
    ACTION_UPDATE_BOT: "Обновление StackSentry запущено",
}

ACTION_RUNNING: dict[str, str] = {
    ACTION_APT_UPDATE: "Обновление списка пакетов",
    ACTION_UPGRADE: "Обновление системы",
    ACTION_UPGRADE_PHASED: "Обновление (phased)",
    ACTION_RELEASE_UPGRADE: "Релиз-обновление ОС",
}

CONFIRM_DESCRIPTIONS: dict[str, str] = {
    ACTION_RESTART_SERVICE: "Перезапустить сервис",
    ACTION_RESTART_XRAY: "Перезапустить xray через API панели",
    ACTION_APT_UPDATE: "Обновить список пакетов (apt update)",
    ACTION_UPGRADE: "Поставить apt update + full-upgrade в очередь systemd",
    ACTION_UPGRADE_PHASED: "Поставить apt update + upgrade (phased) в очередь systemd",
    ACTION_DIST_UPGRADE: "Проверить доступность релиз-обновления ОС",
    ACTION_RELEASE_UPGRADE: (
        "Запустить do-release-upgrade (обновление версии ОС; надолго)"
    ),
    ACTION_REBOOT: "Перезагрузить сервер",
    ACTION_UPDATE_BOT: (
        "Обновить StackSentry из main (установщик перезапустит бота; "
        "конфиг и состояние сохранятся)"
    ),
}

UPDATE_NOTIFIED_KEY = "update_notified_sha"
