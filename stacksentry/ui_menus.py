"""Клавиатуры и меню StackSentry."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .settings import Settings
from .ui_common import (
    LOG_WITH_ERRORS,
    LOG_WITH_MORE,
    PANEL_RESTART,
    PKG_UPDATE_LABELS,
    PKG_UPGRADE_LABELS,
    SUBSCRIPTIONS_PAGE,
)
from .updates import UpdateInfo


def subscriptions_menu(
    offset: int,
    *,
    has_more: bool,
) -> InlineKeyboardMarkup:
    """Постраничная навигация по подпискам плюс выход в главное меню."""
    rows: list[list[InlineKeyboardButton]] = []
    pager: list[InlineKeyboardButton] = []
    if offset > 0:
        pager.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"subs:{max(0, offset - SUBSCRIPTIONS_PAGE)}",
            )
        )
    if has_more:
        pager.append(
            InlineKeyboardButton(
                "📄 Ещё", callback_data=f"subs:{offset + SUBSCRIPTIONS_PAGE}"
            )
        )
    if pager:
        rows.append(pager)
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def update_menu(info: UpdateInfo | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if info is not None and info.update_available is True:
        rows.append(
            [
                InlineKeyboardButton(
                    "⬆️ Обновить StackSentry", callback_data="ask:update_bot:"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔄 Проверить снова", callback_data="update_check")]
    )
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def version_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Проверить обновления", callback_data="update_check"
                )
            ],
            [home_button()],
        ]
    )


def help_menu(*, webapp_url: str = "") -> InlineKeyboardMarkup:
    """Помощь как хаб: версия, обновления и Mini App в одном месте."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "🔄 Проверить обновления", callback_data="update_check"
            ),
            InlineKeyboardButton("🛰 Версия", callback_data="version"),
        ]
    ]
    if webapp_url:
        rows.append(
            [
                InlineKeyboardButton(
                    "📱 Открыть Mini App",
                    web_app=WebAppInfo(url=webapp_url),
                )
            ]
        )
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def home_button() -> InlineKeyboardButton:
    """Кнопка выхода в главное меню — есть на каждом экране, где её легко потерять."""
    return InlineKeyboardButton("🏠 В меню", callback_data="home")


def main_menu(
    is_admin: bool, *, settings: Settings | None = None
) -> InlineKeyboardMarkup:
    """Главное меню подстраивается под настроенную панель.

    Без API панели нет смысла показывать «Трафик» и «Подписки»: они всё равно
    пустые. Кнопка «Панель» остаётся, чтобы объяснить, как включить API.
    """
    panel_enabled = True if settings is None else settings.panel_enabled
    buttons = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")],
        [
            InlineKeyboardButton("🖥 Сервер", callback_data="server"),
            InlineKeyboardButton("⚙️ Сервисы", callback_data="services"),
        ],
    ]
    if (
        is_admin
        and settings is not None
        and settings.webapp_enabled
        and settings.webapp_public_origin
    ):
        buttons.append(
            [
                InlineKeyboardButton(
                    "📱 Открыть Mini App",
                    web_app=WebAppInfo(url=settings.webapp_public_origin),
                )
            ]
        )
    if panel_enabled:
        buttons.append(
            [
                InlineKeyboardButton("🌐 Панель", callback_data="panel"),
                InlineKeyboardButton("📊 Трафик", callback_data="traffic"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton("🪪 Подписки", callback_data="subs"),
                InlineKeyboardButton("📈 История", callback_data="history"),
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton("🌐 Панель (не настроена)", callback_data="panel"),
                InlineKeyboardButton("📈 История", callback_data="history"),
            ]
        )
    if is_admin:
        buttons.append(
            [InlineKeyboardButton("🛠 Обслуживание", callback_data="maintenance")]
        )
        buttons.append(
            [
                InlineKeyboardButton("🔐 Безопасность", callback_data="security"),
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
            ]
        )
    else:
        buttons.append([InlineKeyboardButton("❓ Помощь", callback_data="help")])
    return InlineKeyboardMarkup(buttons)


def security_menu(elevated: bool) -> InlineKeyboardMarkup:
    if elevated:
        action = InlineKeyboardButton(
            "🔒 Заблокировать сейчас", callback_data="security:lock"
        )
    else:
        action = InlineKeyboardButton(
            "🔓 Разблокировать действия", callback_data="security:unlock"
        )
    return InlineKeyboardMarkup(
        [
            [action],
            [InlineKeyboardButton("🔄 Обновить", callback_data="security")],
            [home_button()],
        ]
    )


def twofa_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ Отмена", callback_data="security:cancel"),
                InlineKeyboardButton("❓ Где взять код", callback_data="security:help"),
            ],
            [home_button()],
        ]
    )


def services_menu(
    units: tuple[str, ...],
    is_admin: bool,
    *,
    settings: Settings | None = None,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if is_admin:
        for unit in units:
            # Рестарт и журнал одного юнита — в одной строке: в 3 часа ночи
            # незачем ходить за журналом в отдельное меню.
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"♻️ {unit}", callback_data=f"ask:restart_service:{unit}"
                    ),
                    InlineKeyboardButton(
                        f"📜 Журнал {unit}", callback_data=f"logs:{unit}"
                    ),
                ]
            )
        # Кнопка рестарта ядра — только если панель это умеет.
        if settings is None or (
            settings.panel_enabled and settings.panel_type in PANEL_RESTART
        ):
            label = "♻️ Рестарт xray"
            if settings is not None and settings.panel_type == "s-ui":
                label = "♻️ Рестарт sing-box"
            elif settings is not None and settings.panel_type in {
                "marzban",
                "pasarguard",
            }:
                label = "♻️ Рестарт ядра"
            buttons.append(
                [InlineKeyboardButton(label, callback_data="ask:restart_xray:")]
            )
    buttons.append([InlineKeyboardButton("🧩 Стек сервера", callback_data="stack")])
    buttons.append(
        [
            InlineKeyboardButton("🔄 Обновить", callback_data="services"),
            home_button(),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def log_variants_menu(base: str) -> InlineKeyboardMarkup:
    """Кнопки для лога, запущенного через /run."""
    row: list[InlineKeyboardButton] = []
    if base in LOG_WITH_ERRORS:
        row.append(
            InlineKeyboardButton("🔎 Только ошибки", callback_data=f"logerr:{base}")
        )
    if base in LOG_WITH_MORE:
        row.append(
            InlineKeyboardButton("📜 Ещё 200 строк", callback_data=f"logmore:{base}")
        )
    rows = [row] if row else []
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def logs_menu(units: tuple[str, ...]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"📜 {unit}", callback_data=f"logs:{unit}")]
        for unit in units
    ]
    buttons.append([InlineKeyboardButton("🔙 Сервисы", callback_data="services")])
    buttons.append([home_button()])
    return InlineKeyboardMarkup(buttons)


def maintenance_menu(*, package_manager: str | None = None) -> InlineKeyboardMarkup:
    """Пункты обслуживания под пакетный менеджер сервера.

    Обновление индексов и системы работает для всех поддерживаемых PM;
    phased- и релиз-обновления остаются только у APT.
    """
    pm = package_manager or ""
    # Без явного аргумента сохраняем прежнее поведение (APT-набор) для тестов
    # и старых вызовов; реальный неизвестный PM не получает apt-кнопки.
    apt = package_manager is None or package_manager == "apt"
    update_label = PKG_UPDATE_LABELS.get(pm, "🔄 Обновить список пакетов")
    upgrade_label = PKG_UPGRADE_LABELS.get(pm, "📦 Обновить систему")
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(update_label, callback_data="ask:apt_update:")],
        [InlineKeyboardButton(upgrade_label, callback_data="ask:apt_upgrade:")],
    ]
    if apt:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        "⬆️ Обновить с phased (обычный upgrade)",
                        callback_data="ask:apt_upgrade_phased:",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🚀 Запустить релиз-обновление ОС",
                        callback_data="ask:release_upgrade:",
                    )
                ],
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "💻 Выполнить команду", callback_data="command_prompt"
                ),
                InlineKeyboardButton("📜 Журнал", callback_data="logs_menu"),
            ],
            [
                InlineKeyboardButton(
                    "🔻 Перезагрузить сервер", callback_data="ask:reboot:"
                )
            ],
            [home_button()],
        ]
    )
    return InlineKeyboardMarkup(rows)


def result_menu(
    extra: list[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура после действия: контекст + выход, а не только «домой»."""
    rows = [extra] if extra else []
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def alert_menu(
    actions: list[InlineKeyboardButton] | None = None,
    *,
    mute_key: str = "",
) -> InlineKeyboardMarkup:
    """Кнопки под алертом: починить, посмотреть, «знаю, до утра»."""
    rows: list[list[InlineKeyboardButton]] = []
    if actions:
        rows.append(actions)
    rows.append(
        [
            InlineKeyboardButton("📊 Текущее состояние", callback_data="refresh"),
            InlineKeyboardButton("🏠 В меню", callback_data="home"),
        ]
    )
    if mute_key:
        rows.append(
            [InlineKeyboardButton("😴 Знаю, до утра", callback_data=f"mute:{mute_key}")]
        )
    return InlineKeyboardMarkup(rows)


def overview_menu(
    is_admin: bool,
    *,
    compact: bool,
    settings: Settings | None = None,
) -> InlineKeyboardMarkup:
    """Главное меню плюс переключатель «подробнее/свернуть»."""
    toggle = InlineKeyboardButton(
        "📋 Подробнее" if compact else "🔼 Свернуть",
        callback_data="details" if compact else "refresh",
    )
    return InlineKeyboardMarkup(
        [[toggle], *main_menu(is_admin, settings=settings).inline_keyboard]
    )


def log_view_menu(unit: str) -> InlineKeyboardMarkup:
    """Журнал: фильтр ошибок и добор строк, а не простыня текста."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 Только ошибки", callback_data=f"logs_err:{unit}"
                ),
                InlineKeyboardButton(
                    "📜 Ещё 200 строк", callback_data=f"logs200:{unit}"
                ),
            ],
            [
                InlineKeyboardButton("🔙 Сервисы", callback_data="services"),
                home_button(),
            ],
        ]
    )


def audit_menu() -> InlineKeyboardMarkup:
    """Разбор инцидента: отказы отдельно от действий."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔎 Отказы", callback_data="audit:denied"),
                InlineKeyboardButton("🛠 Действия", callback_data="audit:actions"),
            ],
            [
                InlineKeyboardButton("📜 Всё подряд", callback_data="audit:all"),
                home_button(),
            ],
        ]
    )


def confirm_menu(action: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                # Отмена слева: опасное действие не должно стоять первым там,
                # куда попадает палец при быстром нажатии.
                InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}"),
                InlineKeyboardButton(
                    "✅ Подтвердить", callback_data=f"do:{action}:{token}"
                ),
            ]
        ]
    )
