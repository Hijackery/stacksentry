"""Telegram-бот мониторинга сервера и панели 3X-UI."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import time
from contextlib import suppress
from datetime import timedelta
from typing import Any

from telegram import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    Update,
    User,
    WebAppInfo,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from . import ui
from .actions import (
    ACTION_COMMAND,
    ACTION_DIST_UPGRADE,
    ACTION_REBOOT,
    ACTION_RELEASE_UPGRADE,
    ACTION_RESTART_SERVICE,
    ACTION_RESTART_XRAY,
    ACTION_UPDATE_BOT,
    ACTION_UPGRADE_PHASED,
    ActionError,
    ActionManager,
)
from .alerts import AlertEngine
from .app_commands import CommandMixin
from .app_constants import (
    ACTION_TITLES,
    CRITICAL_ACTIONS,
    CRITICAL_FRESH_WINDOW,
    HEAVY_COOLDOWN,
    MUTE_HOURS,
    PROTECTED_ACTIONS,
    QUEUED_ACTIONS,
    REFRESH_COOLDOWN,
    SNAPSHOT_MAX_AGE,
    TWOFA_PROMPT_TTL,
)
from .app_constants import (
    CONFIRM_DESCRIPTIONS as CONFIRM_DESCRIPTIONS,
)
from .app_jobs import JobsMixin
from .app_utils import (
    _alert_actions as _alert_actions,
)
from .app_utils import (
    _chat_id,
    _message_of,
    _safe_edit,
    _safe_endpoint,
    _user_id,
)
from .app_utils import (
    _JsonScrubbingFormatter as _JsonScrubbingFormatter,
)
from .app_utils import (
    _parse_apt_status as _parse_apt_status,
)
from .app_utils import (
    _ScrubbingFormatter as _ScrubbingFormatter,
)
from .app_utils import (
    _send_with_retry as _send_with_retry,
)
from .app_utils import (
    _split as _split,
)
from .app_utils import (
    _split_alerts as _split_alerts,
)
from .inventory import detect_stack, platform_info
from .models import MonitorSnapshot
from .monitor import Monitor
from .settings import ConfigError, Settings, load_settings
from .storage import Storage, StorageError
from .totp import ReplayGuard, normalize_totp, verify_totp

#: PTB обобщает Application шестью параметрами; конкретные типы здесь не важны.
BotApplication = Application[Any, Any, Any, Any, Any, Any]

logger = logging.getLogger(__name__)


class MonitorBot(CommandMixin, JobsMixin):
    """Собирает хендлеры, фоновые задачи и подтверждение опасных действий."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.state_path)
        self.monitor = Monitor(settings)
        self.engine = AlertEngine(settings, self.storage)
        self.actions = ActionManager(settings)
        #: ОС/пакетный менеджер: меню обслуживания подстраивается под сервер.
        self.platform = platform_info()
        self._snapshot: MonitorSnapshot | None = None
        self._snapshot_at: float = 0.0
        self._snapshot_lock = asyncio.Lock()
        #: Последнее обращение к «тяжёлым» командам (журнал через sudo).
        #: Троттлинг по «ведрам»: {"journal": {user_id: время}, "refresh": {...}}
        self._throttle_seen: dict[str, dict[int, float]] = {}
        #: Последний «экран» в чате: при переходе к новому меню старый удаляем,
        #: чтобы не плодить вереницу сообщений. Id также хранится в базе и
        #: переживает рестарт бота.
        self._last_screen: dict[int, int] = {}
        self._screen_locks: dict[int, asyncio.Lock] = {}
        #: Кто сейчас ищет строку в журнале: user_id -> unit.
        self._log_searches: dict[int, str] = {}
        #: Step-up 2FA: ожидание кода, неудачи и блокировка после перебора.
        self._totp_replay = ReplayGuard(ttl=120.0, claim=self.storage.claim_replay)
        self._totp_failures: dict[int, list[float]] = {}
        self._totp_locked: dict[int, float] = {}
        self._pending_2fa: dict[int, dict[str, Any]] = {}
        self.app: BotApplication = self._build()

    def _main_menu(self, user_id: int | None) -> InlineKeyboardMarkup:
        """Главное меню с учётом настроенной панели (тип/включена ли)."""
        return ui.main_menu(self.is_admin(user_id), settings=self.settings)

    def _maintenance_text(self) -> str:
        """Текст обслуживания: честно про пакетный менеджер этого сервера."""
        base = (
            "🛠 <b>Обслуживание</b>\n\n"
            "Все действия требуют подтверждения и пишутся в журнал."
        )
        pm = self.platform.package_manager
        if not pm or pm == "apt":
            return base
        title = ui.PACKAGE_MANAGER_TITLES.get(pm, pm)
        return (
            "🛠 <b>Обслуживание</b>\n\n"
            f"Пакетный менеджер: <b>{ui.esc(title)}</b>.\n"
            "Обновление индексов и системы выполняется его штатными командами. "
            "Phased- и релиз-обновления доступны только на APT.\n\n"
            "Все действия требуют подтверждения и пишутся в журнал."
        )

    def _build(self) -> BotApplication:
        app = (
            ApplicationBuilder()
            .token(self.settings.telegram_token)
            # Свой Bot API (локальный сервер, корпоративный прокси): базовый URL
            # плюс «/bot», как у официального api.telegram.org.
            .base_url(f"{self.settings.telegram_api_url.rstrip('/')}/bot")
            .concurrent_updates(False)
            # HTML по умолчанию: разметка в отчётах не требует указания mode
            # в каждом вызове send_message.
            .defaults(Defaults(parse_mode=ParseMode.HTML))
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        app.bot_data["bot"] = self
        for name, handler in (
            ("start", self.cmd_start),
            ("help", self.cmd_help),
            ("status", self.cmd_status),
            ("services", self.cmd_services),
            ("panel", self.cmd_panel),
            ("subs", self.cmd_subs),
            ("history", self.cmd_history),
            ("audit", self.cmd_audit),
            ("logs", self.cmd_logs),
            ("maint", self.cmd_maintenance),
            ("stack", self.cmd_stack),
            ("version", self.cmd_version),
            ("updatecheck", self.cmd_updatecheck),
            ("security", self.cmd_security),
            ("traffic", self.cmd_traffic),
            ("run", self.cmd_run),
        ):
            app.add_handler(CommandHandler(name, handler))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)

        job_queue = app.job_queue
        if job_queue is None:  # требуется python-telegram-bot[job-queue]
            raise RuntimeError(
                "JobQueue недоступен: поставьте python-telegram-bot[job-queue]"
            )
        job_queue.run_repeating(
            self.job_check,
            interval=timedelta(seconds=self.settings.check_interval),
            first=timedelta(seconds=10),
            name="periodic-check",
        )
        if self.settings.update_notify:
            job_queue.run_repeating(
                self.job_update_check,
                interval=timedelta(seconds=self.settings.update_check_interval),
                first=timedelta(minutes=2),
                name="update-notify",
            )
        job_queue.run_repeating(
            self.job_prune,
            interval=timedelta(hours=6),
            first=timedelta(minutes=10),
            name="prune",
        )
        return app

    async def _post_init(self, _app: BotApplication) -> None:
        logger.info(
            "Бот запущен. Сервисы: %s, панель: %s, админов: %s",
            ", ".join(self.settings.services),
            _safe_endpoint(self.settings.api_base),
            len(self.settings.admin_ids),
        )
        await self._verify_after_reboot()
        await self._resume_apt_watches()
        await self._flush_alert_outbox()
        if self.settings.webapp_enabled and self.settings.webapp_public_origin:
            try:
                await self.app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Панель",
                        web_app=WebAppInfo(url=self.settings.webapp_public_origin),
                    )
                )
            except TelegramError as exc:
                logger.warning("Не удалось поставить кнопку Mini App: %s", exc)
        else:
            # Если Mini App выключили, убираем старую WebApp-кнопку у скрепки.
            try:
                await self.app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
            except TelegramError as exc:
                logger.warning("Не удалось сбросить кнопку Mini App: %s", exc)

    async def _post_shutdown(self, _app: BotApplication) -> None:
        await self.monitor.close()
        self.storage.close()
        logger.info("Бот остановлен")

    async def snapshot(self, *, force: bool = False) -> MonitorSnapshot:
        """Отдаёт свежий снимок или кэш, если он не старше SNAPSHOT_MAX_AGE.

        Lock нужен, чтобы одновременные команды не запускали дорогой сбор
        несколько раз: пока один запрос обновляет снимок, остальные ждут и
        получают уже готовый результат.
        """
        age = time.monotonic() - self._snapshot_at
        if not force and self._snapshot is not None and age < SNAPSHOT_MAX_AGE:
            return self._snapshot
        async with self._snapshot_lock:
            age = time.monotonic() - self._snapshot_at
            if not force and self._snapshot is not None and age < SNAPSHOT_MAX_AGE:
                return self._snapshot
            snapshot = await self.monitor.snapshot()
            self._snapshot = snapshot
            self._snapshot_at = time.monotonic()
            return snapshot

    def invalidate(self) -> None:
        self._snapshot = None
        self._snapshot_at = 0.0

    def is_admin(self, user_id: int | None) -> bool:
        return self.settings.is_admin(user_id)

    async def _notify(
        self,
        chat_id: int,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
        thread_id: int | None = None,
    ) -> bool:
        chunks = _split(text, 3500)
        # В личном чате администратора весь вывод живёт в одном сообщении:
        # алерты и отчёты не должны оставлять после себя вереницу меню.
        if (
            chat_id in self.settings.admin_ids
            and thread_id is None
            and len(chunks) == 1
        ):
            return bool(await self._show_screen(chat_id, text, markup=markup))
        delivered = True
        for chunk in chunks:
            try:
                await _send_with_retry(
                    self.app.bot.send_message,
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    **({"message_thread_id": thread_id} if thread_id else {}),
                )
            except TelegramError as exc:
                logger.error(
                    "Не удалось отправить сообщение в чат %s: %s",
                    _mask_id(chat_id),
                    exc,
                )
                delivered = False
        return delivered

    async def _alert_targets(self, severity: str = "") -> list[tuple[int, int | None]]:
        """Маршрут алерта: отдельный чат/topic по severity или общий fallback."""
        if severity == "critical" and self.settings.alert_critical_chat_id:
            return [
                (
                    self.settings.alert_critical_chat_id,
                    self.settings.alert_critical_topic_id,
                )
            ]
        if severity in {"warning", "unknown", "ok"} and (
            self.settings.alert_warning_chat_id
        ):
            return [
                (
                    self.settings.alert_warning_chat_id,
                    self.settings.alert_warning_topic_id,
                )
            ]
        if self.settings.alert_chat_id:
            return [(self.settings.alert_chat_id, None)]
        return [(chat_id, None) for chat_id in self.settings.admin_ids]

    async def on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error("Ошибка при обработке апдейта", exc_info=context.error)
        message = getattr(update, "effective_message", None)
        if isinstance(message, Message):
            await message.reply_text(
                "😔 Внутренняя ошибка. Повторите команду; если повторяется — "
                "журнал бота:\n<code>journalctl -u stacksentry-bot -n 50</code>",
                parse_mode=ParseMode.HTML,
            )

    async def _guard(self, message: Message, user: User | None) -> bool:
        """Пропускает администратора в личном чате; отказ пишет в журнал."""
        if not self._chat_allowed(message.chat):
            await message.reply_text(
                "⛔ Команды принимаю только в личном чате администратора."
            )
            return False
        if self.is_admin(user.id if user else None):
            return True
        await self._audit_denied(user, message.text or "")
        await message.reply_text("⛔ Только для администраторов")
        return False

    def _chat_allowed(self, chat: Chat) -> bool:
        """Ответы сервера не должны попадать в группы и каналы.

        Проверка по ID админа пропускает его же команды из группы, поэтому
        тип чата приходится проверять отдельно: иначе /status выложит в чат
        адреса, подписки и трафик.
        """
        # Только личка. alert_chat_id разрешает исходящие алерты в группу, но
        # не управление: иначе /status и подписки увидели бы все участники.
        return chat.type == ChatType.PRIVATE

    async def _audit_denied(self, user: User | None, text: str) -> None:
        """Попытка доступа не-администратора: след в журнале для разбора."""
        try:
            await asyncio.to_thread(
                self.storage.audit,
                "denied",
                telegram_id=user.id if user else None,
                ok=False,
                result=f"команда {text[:40]!r} от не-администратора",
            )
        except sqlite3.Error as exc:  # журнал не должен ломать отказ
            logger.warning("Не удалось записать отказ в журнал: %s", exc)

    def _snapshot_age(self) -> float:
        """Сколько секунд назад собран текущий снимок (0 — только что)."""
        if self._snapshot_at <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self._snapshot_at)

    def _throttle(self, bucket: str, key: int, seconds: float) -> float:
        """Пауза для (ведро, ключ): 0 — можно выполнять, иначе остаток."""
        now = time.monotonic()
        seen = self._throttle_seen.setdefault(bucket, {})
        remaining = seconds - (now - seen.get(key, 0.0))
        if remaining > 0:
            return remaining
        seen[key] = now
        return 0.0

    def _heavy_wait(self, user_id: int) -> float:
        """Сколько секунд осталось до следующего тяжёлого запроса (0 — можно)."""
        return self._throttle("journal", user_id, HEAVY_COOLDOWN)

    async def _answer(
        self,
        query: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        """Ответ на кнопку не должен ронять хендлер (кнопка могла устареть)."""
        try:
            await query.answer(text=text, show_alert=show_alert)
        except TelegramError as exc:
            logger.debug("Не удалось ответить на кнопку: %s", exc)

    async def on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query: CallbackQuery | None = update.callback_query
        if query is None or query.data is None:
            return
        await self._answer(query)
        data = query.data
        user_id = query.from_user.id
        # Любая кнопка отменяет незавершённый поиск по журналу.
        self._log_searches.pop(user_id, None)

        # Команды и кнопки — только в личном чате (см. _chat_allowed).
        message = query.message
        if isinstance(message, Message) and not self._chat_allowed(message.chat):
            await self._audit_denied(
                query.from_user, f"callback {data[:40]!r} из группы"
            )
            await self._answer(
                query, "⛔ Кнопки работают только в личном чате", show_alert=True
            )
            return

        # Гейт стоит до разбора callback_data: иначе гость, угадав токен
        # подтверждения, мог отменить действие администратора.
        if not self.is_admin(user_id):
            await self._audit_denied(query.from_user, f"callback {data[:40]!r}")
            await self._answer(query, "⛔ Только для администраторов")
            await self._edit(query, "⛔ Только для администраторов.")
            return
        # Inline-сообщения не имеют чата, но их по-прежнему может нажать только
        # администратор (проверено выше). Остальные недоступные сообщения
        # отклоняем, чтобы нельзя было обойти проверку личного чата.
        if not isinstance(message, Message) and not query.inline_message_id:
            await self._answer(query, "⛔ Это сообщение недоступно")
            return

        # «Обновить»/«В меню» трогают панель и systemctl: сначала проверяем
        # паузу, и только потом показываем «Собираю данные…». Иначе при
        # сработавшем throttle экран оставался без клавиатуры.
        if data in {"home", "refresh"}:
            wait = self._throttle("refresh", user_id, REFRESH_COOLDOWN)
            if wait > 0:
                await self._answer(query, f"⏳ Обновление через {wait:.0f} с")
                return
            await self._edit(query, "⏳ Собираю данные…")

        # Мгновенный отклик: сбор данных может занять секунды (медленная
        # панель), и без этой строки кажется, что бот завис.
        if data in {
            "server",
            "panel",
            "traffic",
            "subs",
            "history",
            "details",
        } or data.startswith("subs:"):
            await self._edit(query, "⏳ Собираю данные…")

        if data.startswith("mute:"):
            key = data[5:]
            until = await asyncio.to_thread(self.storage.mute, key, MUTE_HOURS * 3600)
            await self._edit(
                query,
                f"😴 Понял: <code>{ui.esc(key)}</code> молчит до "
                f"{time.strftime('%H:%M', time.localtime(until))}. "
                "Если проблема пройдёт сама — сообщу, когда срок выйдет.",
                markup=self._main_menu(user_id),
            )
            return

        if data.startswith("ask:"):
            await self._ask_confirmation(query, data)
            return
        if data.startswith("do:"):
            await self._run_confirmed(query, data, user_id)
            return
        if data.startswith("no:"):
            cancelled = self.actions.cancel(data[3:], user_id)
            await self._edit(
                query,
                "❌ Отменено — действие не выполнено."
                if cancelled
                else "⌛ Подтверждение уже неактуально, отменять нечего.",
                markup=self._main_menu(user_id),
            )
            return

        if data in {"home", "refresh"}:
            self.invalidate()
            await self._edit(
                query,
                ui.render_overview(
                    await self.snapshot(force=True),
                    self.settings,
                    age=0.0,
                    compact=True,
                ),
                markup=ui.overview_menu(
                    self.is_admin(user_id),
                    compact=True,
                    settings=self.settings,
                ),
            )
        elif data in {"details", "server"}:
            await self._edit(
                query,
                ui.render_overview(
                    await self.snapshot(), self.settings, age=self._snapshot_age()
                ),
                markup=ui.overview_menu(
                    self.is_admin(user_id),
                    compact=False,
                    settings=self.settings,
                ),
            )
        elif data == "services":
            await self._edit(
                query,
                ui.render_services(await self.snapshot()),
                markup=ui.services_menu(
                    self.settings.services,
                    self.is_admin(user_id),
                    settings=self.settings,
                ),
            )
        elif data == "stack":
            detected = await asyncio.to_thread(detect_stack)
            snapshot = await self.snapshot()
            await self._edit(
                query,
                ui.render_stack(detected, self.settings.services, snapshot.services),
                markup=ui.services_menu(
                    self.settings.services,
                    self.is_admin(user_id),
                    settings=self.settings,
                ),
            )
        elif data == "panel":
            await self._edit(
                query,
                ui.render_panel(
                    await self.snapshot(), panel_type=self.settings.panel_type
                ),
                markup=self._main_menu(user_id),
            )
        elif data == "traffic":
            await self._edit(
                query,
                ui.render_traffic(await self.snapshot()),
                markup=self._main_menu(user_id),
            )
        elif data == "subs" or data.startswith("subs:"):
            offset = (
                int(data[5:]) if data.startswith("subs:") and data[5:].isdigit() else 0
            )
            text, markup = await self._subs_view(offset)
            await self._edit(query, text, markup=markup)
        elif data == "history":
            await self._edit(
                query, await self._history_text(24), markup=ui.history_menu(24)
            )
        elif data.startswith("history:"):
            hours = int(data[8:]) if data[8:].isdigit() else 24
            if hours not in {24, 168, 720}:
                hours = 24
            await self._edit(
                query,
                await self._history_text(hours),
                markup=ui.history_menu(hours),
            )
        elif data == "logs_menu":
            await self._edit(
                query, "📜 Выберите сервис", markup=ui.logs_menu(self.settings.services)
            )
        elif data.startswith("audit:"):
            mode = data[6:]
            rows = await asyncio.to_thread(
                self.storage.recent_audit,
                15,
                only="" if mode == "all" else mode,
            )
            title = {"denied": " (отказы)", "actions": " (действия)"}.get(mode, "")
            await self._edit(
                query,
                ui.render_audit(rows).replace(
                    "Журнал действий", f"Журнал действий{title}"
                ),
                markup=ui.audit_menu(),
            )
        elif data == "help":
            webapp_url = ""
            if (
                self.is_admin(user_id)
                and self.settings.webapp_enabled
                and self.settings.webapp_public_origin
            ):
                webapp_url = self.settings.webapp_public_origin
            await self._edit(
                query,
                ui.render_help(
                    self.is_admin(user_id),
                    settings=self.settings,
                    package_manager=self.platform.package_manager,
                ),
                markup=ui.help_menu(webapp_url=webapp_url),
            )
        elif data == "version":
            await self._show_version(query)
        elif data == "update_check":
            await self._show_update_check(query)
        elif data == "maintenance":
            await self._edit(
                query,
                self._maintenance_text(),
                markup=ui.maintenance_menu(
                    package_manager=self.platform.package_manager
                ),
            )
        elif data.startswith("logsearch:"):
            await self._prompt_log_search(query, data[10:])
        elif data.startswith("logs:"):
            await self._show_logs(query, data[5:])
        elif data.startswith("logs200:"):
            await self._show_logs(query, data[8:], lines=200)
        elif data.startswith("logs_err:"):
            await self._show_logs(query, data[9:], errors=True)
        elif data.startswith("logmore:") or data.startswith("logerr:"):
            await self._show_run_log(query, data, user_id)
        elif data == "command_prompt":
            await self._prompt_command(query, context)
        elif data == "security":
            await self._show_security(query, user_id)
        elif data == "security:unlock":
            await self._start_2fa(query, "unlock", "", user_id, "security")
        elif data == "security:lock":
            await asyncio.to_thread(self.storage.clear_elevation, user_id)
            await self._show_security(query, user_id)
        elif data == "security:cancel":
            self._pending_2fa.pop(user_id, None)
            await self._show_security(query, user_id)
        elif data == "security:help":
            await self._edit(
                query,
                "🔐 Код выдаёт приложение-аутентификатор "
                "(Google Authenticator, Aegis, 1Password и т.п.). "
                "Секрет выдаётся при установке или в "
                "<code>sudo stacksentry-menu</code>. Код меняется каждые 30 с.",
                markup=ui.twofa_menu(),
            )
        else:
            await self._edit(
                query,
                "Неизвестная кнопка. Возможно, клавиатура устарела.",
                markup=self._main_menu(user_id),
            )

    def _confirmation_description(self, action: str, arg: str) -> str | None:
        base = CONFIRM_DESCRIPTIONS.get(action)
        if base is None:
            return None
        description = (
            f"{base} <code>{ui.esc(arg)}</code>?"
            if action == ACTION_RESTART_SERVICE
            else f"{base}?"
        )
        if action == ACTION_REBOOT:
            description = (
                "<b>Перезагрузить сервер?</b> "
                "Бот и панель будут недоступны ~1-2 минуты."
            )
        return description

    def _needs_2fa(self, action: str) -> bool:
        return self.settings.require_2fa and action in PROTECTED_ACTIONS

    def _is_elevated(self, user_id: int, *, fresh: bool = False) -> bool:
        if not self.settings.require_2fa:
            return True
        until = self.storage.elevation_until(user_id)
        now = time.time()
        if until <= now:
            return False
        if fresh:
            return (until - now) > (self.settings.elevation_ttl - CRITICAL_FRESH_WINDOW)
        return True

    def _totp_lock_left(self, user_id: int) -> float:
        return max(0.0, self._totp_locked.get(user_id, 0.0) - time.time())

    def _check_totp(self, user_id: int, code: str) -> tuple[bool, str, int]:
        code = normalize_totp(code)
        lock_left = self._totp_lock_left(user_id)
        if lock_left > 0:
            return (
                False,
                f"Слишком много попыток. Подождите {lock_left:.0f} с.",
                0,
            )
        if not verify_totp(self.settings.totp_secret, code):
            now = time.time()
            failures = [
                item
                for item in self._totp_failures.get(user_id, [])
                if now - item < self.settings.totp_lockout
            ]
            failures.append(now)
            self._totp_failures[user_id] = failures
            left = max(0, self.settings.totp_max_attempts - len(failures))
            if left == 0:
                self._totp_locked[user_id] = now + self.settings.totp_lockout
                self._totp_failures[user_id] = []
            return False, "Неверный код.", left
        if not self._totp_replay.allow(f"{user_id}:{code}"):
            return False, "Этот код уже использован, дождитесь следующего.", 1
        self._totp_failures.pop(user_id, None)
        return True, "", self.settings.totp_max_attempts

    async def _start_2fa(
        self,
        callback: CallbackQuery,
        action: str,
        arg: str,
        user_id: int,
        data: str,
    ) -> None:
        self._pending_2fa[user_id] = {
            "action": action,
            "arg": arg,
            "data": data,
            "expires": time.time() + TWOFA_PROMPT_TTL,
        }
        title = (
            "Разблокировать опасные действия"
            if action == "unlock"
            else self._confirmation_description(action, arg) or action
        )
        await self._edit(
            callback,
            ui.render_2fa_prompt(title, self._totp_lock_left(user_id)),
            markup=ui.twofa_menu(),
        )

    async def _complete_2fa(self, message: Message, user_id: int, code: str) -> None:
        pending = self._pending_2fa.get(user_id)
        if pending is None:
            return
        if pending["expires"] < time.time():
            self._pending_2fa.pop(user_id, None)
            await self._reply_screen(
                message,
                "⌛ Время ввода кода истекло. Начните действие заново.",
                markup=ui.twofa_menu(),
            )
            return
        ok, reason, left = self._check_totp(user_id, code)
        await asyncio.to_thread(
            self.storage.audit,
            "2fa",
            telegram_id=user_id,
            ok=ok,
            result=reason or "ok",
        )
        if not ok:
            suffix = f" Осталось попыток: {left}." if left else ""
            await self._reply_screen(
                message,
                f"❌ {ui.esc(reason)}{suffix}\n\nВведите код ещё раз.",
                markup=ui.twofa_menu(),
            )
            if left == 0:
                self._pending_2fa.pop(user_id, None)
            return
        until = await asyncio.to_thread(
            self.storage.set_elevation, user_id, self.settings.elevation_ttl
        )
        self._pending_2fa.pop(user_id, None)
        await self._reply_screen(
            message,
            f"🔓 Действия разблокированы до "
            f"{time.strftime('%H:%M', time.localtime(until))}.",
        )
        action = str(pending.get("action", ""))
        if action == "unlock":
            await self._reply_screen(
                message,
                ui.render_security(self.settings, until),
                markup=ui.security_menu(True),
            )
            return
        await self._send_confirmation(
            message, action, str(pending.get("arg", "")), user_id
        )

    async def _send_confirmation(
        self, message: Message, action: str, arg: str, user_id: int
    ) -> None:
        description = self._confirmation_description(action, arg)
        if description is None:
            return
        pending = self.actions.request(action, description, arg, owner_id=user_id)
        await self._reply_screen(
            message,
            f"⚠️ {description}",
            markup=ui.confirm_menu(action, pending.token),
        )

    async def _show_security(self, callback: CallbackQuery, user_id: int) -> None:
        until = await asyncio.to_thread(self.storage.elevation_until, user_id)
        await self._edit(
            callback,
            ui.render_security(self.settings, until),
            markup=ui.security_menu(until > time.time()),
        )

    async def cmd_security(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        user_id = _user_id(update) or 0
        until = await asyncio.to_thread(self.storage.elevation_until, user_id)
        await self._reply_screen(
            message,
            ui.render_security(self.settings, until),
            markup=ui.security_menu(until > time.time()),
        )

    async def _ask_confirmation(self, callback: CallbackQuery, data: str) -> None:
        if not self.is_admin(callback.from_user.id):
            await self._edit(callback, "⛔ Только для администраторов.")
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            await self._edit(
                callback,
                "Неизвестная кнопка. Возможно, клавиатура устарела.",
                markup=self._main_menu(callback.from_user.id),
            )
            return
        _, action, arg = parts
        description = self._confirmation_description(action, arg)
        if description is None:
            await self._edit(
                callback,
                "Неизвестное действие. Возможно, клавиатура устарела.",
                markup=self._main_menu(callback.from_user.id),
            )
            return
        user_id = callback.from_user.id
        if self._needs_2fa(action) and not self._is_elevated(
            user_id, fresh=action in CRITICAL_ACTIONS
        ):
            await self._start_2fa(callback, action, arg, user_id, data)
            return

        pending = self.actions.request(action, description, arg, owner_id=user_id)
        await self._edit(
            callback,
            f"⚠️ {description}",
            markup=ui.confirm_menu(action, pending.token),
        )

    async def _run_confirmed(
        self, callback: CallbackQuery, data: str, user_id: int
    ) -> None:
        if not self.is_admin(user_id):
            await self._edit(callback, "⛔ Только для администраторов.")
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            await self._edit(
                callback,
                "Неизвестная кнопка. Возможно, клавиатура устарела.",
                markup=self._main_menu(user_id),
            )
            return
        _, action, token = parts
        if self._needs_2fa(action) and not self._is_elevated(
            user_id, fresh=action in CRITICAL_ACTIONS
        ):
            await self._edit(
                callback,
                "🔐 Сначала разблокируйте действия: «Безопасность» → "
                "«Разблокировать», затем подтвердите снова.",
                markup=ui.security_menu(False),
            )
            return
        try:
            pending = self.actions.consume(token, user_id, expected=action)
        except ActionError as exc:
            await self._edit(
                callback,
                f"⏳ {ui.esc(str(exc))}",
                markup=self._main_menu(callback.from_user.id),
            )
            return
        if pending is None:
            await self._edit(
                callback,
                "⏳ Подтверждение не найдено или уже использовано.",
                markup=self._main_menu(callback.from_user.id),
            )
            return
        await self._dispatch(callback, pending.action, pending.args, user_id)

    async def _dispatch(
        self,
        query: CallbackQuery,
        action: str,
        args: tuple[str, ...],
        user_id: int,
    ) -> None:
        chat_id = _chat_id(query)
        # Переиспользуем сообщение с подтверждением, а не плодим ещё одно.
        status = _message_of(query)
        await self._adopt_screen(status)
        await _safe_edit(status, "⏳ Выполняю…", markup=None)
        # Кнопки apt-обновлений скрыты на других ОС, но callback_data можно
        # подделать: не запускаем заведомо неверный пакетный менеджер.
        # Обновления индексов и системы теперь кроссплатформенные; phased- и
        # релиз-обновления остаются APT-специфичными.
        if action in {
            ACTION_UPGRADE_PHASED,
            ACTION_RELEASE_UPGRADE,
        } and self.platform.package_manager not in {"", "apt"}:
            await _safe_edit(
                status,
                "❌ Phased/релиз-обновления доступны только на APT "
                f"(пакетный менеджер: {ui.esc(self.platform.package_manager or '?')}).",
                markup=ui.result_menu(),
            )
            return
        if action == ACTION_RESTART_XRAY and (
            not self.settings.panel_enabled
            or self.settings.panel_type not in ui.PANEL_RESTART
        ):
            await _safe_edit(
                status,
                "❌ Перезапуск ядра недоступен для настроенной панели "
                f"({ui.esc(self.settings.panel_type)}).",
                markup=ui.result_menu(),
            )
            return
        if chat_id:
            self._last_screen[chat_id] = status.message_id
        self.invalidate()

        try:
            if action == ACTION_REBOOT:
                await self._do_reboot(status, user_id)
                return
            if action == ACTION_UPDATE_BOT:
                result = await self.actions.update_bot()
                if result.ok:
                    await _safe_edit(
                        status,
                        "⬆️ <b>Обновление запущено.</b>\n"
                        "StackSentry скачает main, сохранит .env и состояние, "
                        "затем перезапустит бота. Через минуту проверьте "
                        "<code>/version</code> или «Проверить обновления».",
                        markup=ui.result_menu(),
                    )
                else:
                    await _safe_edit(
                        status,
                        "❌ Не удалось запустить обновление: "
                        f"<code>{ui.esc(result.summary(500))}</code>",
                        markup=ui.result_menu(),
                    )
                await asyncio.to_thread(
                    self.storage.audit,
                    action,
                    telegram_id=user_id,
                    ok=result.ok,
                    result=result.summary(500),
                )
                return
            if action in QUEUED_ACTIONS or action == ACTION_DIST_UPGRADE:
                self._schedule_long_action(
                    action, chat_id, user_id, message_id=status.message_id
                )
                await _safe_edit(
                    status,
                    "⏳ Проверяю…"
                    if action == ACTION_DIST_UPGRADE
                    else "⏳ Ставлю задачу в очередь…",
                    markup=ui.result_menu(),
                )
                return
            if action == ACTION_COMMAND:
                result = await self.actions.run_allowed_command(args[0])
            elif action == ACTION_RESTART_SERVICE:
                result = await self.actions.restart_service(args[0])
                self.monitor.services.forget(args[0])
            elif action == ACTION_RESTART_XRAY:
                result = await self.actions.restart_xray()
            else:
                raise ActionError(f"Неизвестное действие: {action}")
        except Exception as exc:  # noqa: BLE001 — сбой возвращаем в чат, не «зависаем»
            logger.error("Действие %s не удалось: %s", action, exc, exc_info=True)
            await asyncio.to_thread(
                self.storage.audit,
                action,
                telegram_id=user_id,
                ok=False,
                result=str(exc) or exc.__class__.__name__,
            )
            await _safe_edit(
                status,
                f"❌ {ui.esc(str(exc) or exc.__class__.__name__)}",
                markup=ui.result_menu(),
            )
            return

        icon = "✅" if result.ok else "⚠️"
        title = ACTION_TITLES.get(action, action)
        if action == ACTION_RESTART_XRAY:
            if self.settings.panel_type == "s-ui":
                title = "sing-box перезапущен"
            elif self.settings.panel_type in {"marzban", "pasarguard"}:
                title = "Ядро панели перезапущено"
        body = ui.esc(result.summary(1500))
        extra: list[InlineKeyboardButton] = []
        if action == ACTION_RESTART_SERVICE and args:
            unit = args[0]
            extra.append(
                InlineKeyboardButton(f"📜 Журнал {unit}", callback_data=f"logs:{unit}")
            )
        tail = "" if result.ok else f"\nКод возврата: {result.returncode}"
        markup = ui.result_menu(extra)
        if action == ACTION_COMMAND and args and args[0] in ui.LOG_WITH_ERRORS:
            markup = ui.log_variants_menu(args[0])
        await _safe_edit(
            status,
            f"{icon} <b>{ui.esc(title)}</b> за {result.duration:.1f} с\n"
            f"<code>{ui.esc(result.command)}</code>\n\n<pre>{body}</pre>{tail}",
            markup=markup,
        )
        await asyncio.to_thread(
            self.storage.audit,
            action,
            telegram_id=user_id,
            params={"args": list(args)},
            ok=result.ok,
            result=result.summary(500),
        )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команду сервера можно прислать в любом разделе, без /maint."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        if not self._chat_allowed(message.chat) or not self.is_admin(user.id):
            return
        if user.id in self._pending_2fa:
            code = "".join(ch for ch in (message.text or "") if ch.isdigit())
            if len(code) != 6:
                await self._reply_screen(
                    message,
                    "Нужны 6 цифр из приложения-аутентификатора или нажмите «Отмена».",
                    markup=ui.twofa_menu(),
                )
                return
            try:
                await message.delete()
            except TelegramError as exc:
                logger.debug("Не удалось удалить сообщение с кодом: %s", exc)
            await self._complete_2fa(message, user.id, code)
            return
        pending_search = self._log_searches.pop(user.id, None)
        if pending_search is not None:
            await self._run_log_search(
                message, user.id, pending_search, message.text or ""
            )
            return
        command = " ".join((message.text or "").split())
        if not command:
            return
        user_data = context.user_data or {}
        awaiting = bool(user_data.pop("awaiting_command", False))
        allowed = self.settings.command_allowlist
        if command in allowed:
            await self._request_command(message, user.id, command)
        elif awaiting:
            listed = ", ".join(allowed) or "пусто"
            await self._reply_screen(
                message,
                "⛔ Команда не в белом списке.\n"
                f"Разрешено: <code>{ui.esc(listed)}</code>",
                markup=self._main_menu(True),
            )

    async def _prompt_log_search(self, callback: CallbackQuery, unit: str) -> None:
        if not self.is_admin(callback.from_user.id):
            await self._edit(callback, "⛔ Только для администраторов.")
            return
        if not unit:
            await self._edit(
                callback,
                "Не выбран журнал.",
                markup=ui.logs_menu(self.settings.services),
            )
            return
        self._log_searches[callback.from_user.id] = unit
        await self._edit(
            callback,
            f"🔎 <b>Поиск в журнале</b> <code>{ui.esc(unit)}</code>\n\n"
            "Пришлите текст для поиска. Любая кнопка отменит поиск.",
            markup=ui.log_view_menu(unit),
        )

    async def _run_log_search(
        self, message: Message, user_id: int, unit: str, query: str
    ) -> None:
        needle = " ".join(query.split())
        if not needle:
            await self._reply_screen(
                message,
                "Пустой запрос. Поиск отменён.",
                markup=ui.log_view_menu(unit),
            )
            return
        wait = self._heavy_wait(user_id)
        if wait > 0:
            await self._reply_screen(
                message,
                f"Журнал: следующий запрос через {wait:.0f} с",
                markup=ui.log_view_menu(unit),
            )
            return
        try:
            output = await self.actions.journal(unit, 200, errors=False)
        except ActionError as exc:
            await self._reply_screen(
                message, f"❌ {ui.esc(str(exc))}", markup=ui.log_view_menu(unit)
            )
            return
        lines = [line for line in output.splitlines() if needle.lower() in line.lower()]
        if not lines:
            text = (
                f"🔎 По запросу <code>{ui.esc(needle)}</code> ничего не найдено "
                f"в <code>{ui.esc(unit)}</code>."
            )
        else:
            body = "\n".join(lines[-80:])
            text = (
                f"🔎 <b>{ui.esc(unit)}</b> · найдено {len(lines)}\n"
                f"<pre>{ui.esc(body[-3000:])}</pre>"
            )
        await self._reply_screen(message, text, markup=ui.log_view_menu(unit))

    async def _prompt_command(
        self,
        callback: CallbackQuery,
        context: CallbackContext[Any, Any, Any, Any],
    ) -> None:
        if not self.is_admin(callback.from_user.id):
            await self._edit(callback, "⛔ Только для администраторов.")
            return
        if context.user_data is not None:
            context.user_data["awaiting_command"] = True
        await self._edit(
            callback,
            ui.render_command_help(self.settings.command_allowlist),
            markup=self._main_menu(True),
        )


def create_app(settings: Settings) -> BotApplication:
    return MonitorBot(settings).app


def main() -> None:
    # Секреты (база, лог) не должны быть доступны другим пользователям хоста,
    # даже если файлы создаются с учёткой по умолчанию.
    os.umask(0o077)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка конфигурации:\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    _setup_logging(settings)
    _warn_on_unusual_api(settings)
    if not settings.panel_verify_tls:
        logger.warning(
            "PANEL_VERIFY_TLS=false: TLS-сертификат панели не проверяется — "
            "бот уязвим к подмене панели в сети. Включайте только для отладки."
        )
    try:
        app = create_app(settings)
    except StorageError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def _warn_on_unusual_api(settings: Settings) -> None:
    """Нестандартный Bot API — повод для предупреждения.

    В `TELEGRAM_API_URL` можно подменить адрес, и тогда токен бота уйдёт на
    чужой сервер. Официальный адрес молчит, любой другой — заметен в логе.
    """
    if settings.telegram_api_url.rstrip("/") != "https://api.telegram.org":
        logger.warning(
            "Используется неофициальный Bot API: %s — токен уходит туда",
            settings.telegram_api_url,
        )


def _mask_id(value: int | None) -> str:
    """Telegram ID в логах — персональные данные: оставляем хвост для сверки."""
    if value is None:
        return "—"
    text = str(value)
    return f"…{text[-3:]}" if len(text) > 3 else "…"


def _setup_logging(settings: Settings) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers.append(logging.FileHandler(settings.log_path, encoding="utf-8"))
    # Файл мог остаться от старой установки с 0644 — принудительно закрываем.
    for path in (settings.log_path.parent, settings.log_path):
        with suppress(OSError):
            os.chmod(path, 0o750 if path.is_dir() else 0o600)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # Поверх всех обработчиков — вырезаем секреты из готовых строк (включая
    # трейсбеки): токен бота из URL и точные значения токенов из конфигурации.
    log_format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    secrets = (
        settings.telegram_token,
        settings.panel_api_token,
        settings.panel_password,
        settings.panel_url,
        settings.totp_secret,
        settings.webapp_secret,
    )
    formatter = (
        _JsonScrubbingFormatter(log_format, secrets)
        if settings.log_json
        else _ScrubbingFormatter(log_format, secrets)
    )
    for handler in handlers:
        handler.setFormatter(formatter)
    # Сторонние библиотеки на DEBUG печатают то, что обязано остаться в тайне:
    # telegram логирует «Set Bot API URL: https://api.telegram.org/bot<TOKEN>»,
    # httpx — полные URL запросов. Приглушаем их независимо от LOG_LEVEL,
    # иначе включённый для отладки DEBUG сливает токен бота в файл лога.
    for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
