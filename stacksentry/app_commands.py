"""Команды Telegram-бота StackSentry."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
import time
from contextlib import suppress
from typing import Any, cast

from telegram import CallbackQuery, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from . import __version__, ui
from .actions import ACTION_COMMAND, LOG_ERRORS, LOG_MORE, ActionError
from .app_base import BotBase
from .app_utils import (
    _message_of,
    _safe_edit,
    _send_with_retry,
    _user_id,
)
from .inventory import detect_stack
from .updates import check_for_update

logger = logging.getLogger(__name__)


class CommandMixin(BotBase):
    """Обработчики команд и общие экраны."""

    async def cmd_start(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not self._chat_allowed(message.chat):
            return
        user = update.effective_user
        allowed = self.is_admin(user.id if user else None)
        # Гостю кнопки не показываем вовсе: каждая из них всё равно ответила бы
        # отказом — это только вводит в заблуждение.
        await self._reply_screen(
            message,
            "🛰 <b>StackSentry</b> — бот мониторинга сервера и панели 3X-UI.\n\n"
            + (
                "Показывает состояние системы, сервисов, панели, подписок и "
                "трафика, а также позволяет перезапускать сервисы и обновлять "
                "систему.\n\nНачните с /status или выберите раздел кнопками."
                if allowed
                else "⛔ Этот бот доступен только администратору сервера."
            ),
            markup=self._main_menu(True) if allowed else None,
        )

    async def cmd_help(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not self._chat_allowed(message.chat):
            return
        allowed = self.is_admin(_user_id(update))
        await self._reply_screen(
            message,
            ui.render_help(
                allowed,
                settings=self.settings,
                package_manager=self.platform.package_manager,
            ),
            markup=self._main_menu(True) if allowed else None,
        )

    async def cmd_status(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        try:
            await message.reply_chat_action(ChatAction.TYPING)
        except TelegramError as exc:
            logger.debug("Не удалось показать «печатает»: %s", exc)
        snapshot = await self.snapshot()
        await self._reply_screen(
            message,
            ui.render_overview(snapshot, self.settings, age=self._snapshot_age()),
            markup=self._main_menu(_user_id(update)),
        )

    async def cmd_stack(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        detected = await asyncio.to_thread(detect_stack)
        snapshot = await self.snapshot()
        await self._reply_screen(
            message,
            ui.render_stack(detected, self.settings.services, snapshot.services),
            markup=ui.services_menu(
                self.settings.services,
                self.is_admin(_user_id(update)),
                settings=self.settings,
            ),
        )

    def _version_text(self) -> str:
        panel = ui.panel_title(self.settings.panel_type)
        panel_state = "API включён" if self.settings.panel_enabled else "monitor-only"
        os_name = self.platform.os_name or self.platform.os_id or "ОС неизвестна"
        return (
            "🛰 <b>StackSentry</b> "
            f"{ui.esc(__version__)}\n"
            f"Python {ui.esc(sys.version.split()[0])} · "
            f"{ui.esc(os_name)}\n"
            f"init: {ui.esc(self.platform.init or '?')} · "
            f"пакеты: {ui.esc(self.platform.package_manager or '?')}\n"
            f"Панель: {ui.esc(panel)} ({panel_state})"
        )

    async def cmd_version(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Версия бота, ОС, пакетный менеджер и выбранная панель."""
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        await self._reply_screen(
            message, self._version_text(), markup=ui.version_menu()
        )

    async def _show_version(self, query: CallbackQuery) -> None:
        await self._edit(query, self._version_text(), markup=ui.version_menu())

    async def cmd_updatecheck(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """`/updatecheck` — сравнить установленный commit с main."""
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        await self._send_update_check(message)

    async def _send_update_check(self, message: Message) -> None:
        await self._show_screen(
            message.chat_id, "⏳ Проверяю обновления…", reply_to=message
        )
        info = await asyncio.to_thread(check_for_update)
        await self._show_screen(
            message.chat_id,
            ui.render_update(info),
            markup=ui.update_menu(info),
            reply_to=message,
        )

    async def _show_update_check(self, query: CallbackQuery) -> None:
        status = _message_of(query)
        await _safe_edit(status, "⏳ Проверяю обновления…", markup=None)
        info = await asyncio.to_thread(check_for_update)
        await _safe_edit(status, ui.render_update(info), markup=ui.update_menu(info))

    async def cmd_services(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        await self._reply_screen(
            message,
            ui.render_services(await self.snapshot()),
            markup=ui.services_menu(
                self.settings.services,
                self.is_admin(_user_id(update)),
                settings=self.settings,
            ),
        )

    async def cmd_panel(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        text = ui.render_panel(
            await self.snapshot(), panel_type=self.settings.panel_type
        )
        update_info: dict[str, Any] = {}
        if self.settings.panel_enabled:
            try:
                update_info = await self.monitor.panel.panel_update_info()
            except Exception as exc:  # доп. информация не критична
                logger.info("getPanelUpdateInfo недоступен: %s", exc)
                update_info = {}
        if update_info:
            latest = update_info.get("tagName") or update_info.get("version") or "?"
            text += f"\nПоследний релиз панели: {ui.esc(latest)}"
        await self._reply_screen(
            message, text, markup=self._main_menu(_user_id(update))
        )

    async def cmd_subs(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        text, markup = await self._subs_view(0)
        await self._reply_screen(message, text, markup=markup)

    async def _subs_view(self, offset: int) -> tuple[str, InlineKeyboardMarkup]:
        """Страница подписок и клавиатура к ней (общая для /subs и кнопок)."""
        subs = (await self.snapshot()).subscriptions
        soon = self.settings.expiring_soon_days
        ordered = ui.ordered_subscriptions(subs, soon)
        if offset < 0 or offset >= len(ordered):
            offset = 0
        has_more = offset + ui.SUBSCRIPTIONS_PAGE < len(ordered)
        return (
            ui.render_subscriptions(subs, soon_days=soon, offset=offset),
            ui.subscriptions_menu(offset, has_more=has_more),
        )

    async def cmd_history(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        rows = await asyncio.to_thread(
            self.storage.get_series, time.time() - 24 * 3600, 24
        )
        await self._reply_screen(
            message,
            ui.render_history(rows, title="Последние 24 часа"),
            markup=self._main_menu(_user_id(update)),
        )

    async def cmd_audit(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        rows = await asyncio.to_thread(self.storage.recent_audit, 15)
        await self._reply_screen(message, ui.render_audit(rows), markup=ui.audit_menu())

    async def cmd_logs(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        await self._reply_screen(
            message,
            "📜 <b>Журналы сервисов</b>",
            markup=ui.logs_menu(self.settings.services),
        )

    async def cmd_maintenance(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        await self._reply_screen(
            message,
            self._maintenance_text(),
            markup=ui.maintenance_menu(package_manager=self.platform.package_manager),
        )

    async def cmd_traffic(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        await self._reply_screen(
            message,
            ui.render_traffic(await self.snapshot()),
            markup=self._main_menu(_user_id(update)),
        )

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/run <команда>` — команда из белого списка из любого раздела."""
        message = update.effective_message
        if message is None or not await self._guard(message, update.effective_user):
            return
        text = " ".join((message.text or "").split())
        arg = text.partition(" ")[2].strip()
        if not arg:
            allowed = (
                "\n".join(
                    f"• <code>{ui.esc(item)}</code>"
                    for item in self.settings.command_allowlist
                )
                or "—"
            )
            await self._reply_screen(
                message,
                "💻 <b>Команда на сервер</b>\n\n"
                "Пришлите команду следующим сообщением в любом разделе или "
                "<code>/run &lt;команда&gt;</code>.\n\n"
                f"Разрешено:\n{allowed}",
                markup=self._main_menu(True),
            )
            return
        if context.user_data is not None:
            context.user_data["awaiting_command"] = True
        await self._request_command(message, _user_id(update) or 0, arg)

    async def _request_command(
        self, message: Message, user_id: int, command: str
    ) -> None:
        if command not in self.settings.command_allowlist:
            allowed = ", ".join(self.settings.command_allowlist) or "пусто"
            await self._reply_screen(
                message,
                "⛔ Команда не в белом списке.\n"
                f"Разрешено: <code>{ui.esc(allowed)}</code>",
                markup=self._main_menu(user_id),
            )
            return
        pending = self.actions.request(
            ACTION_COMMAND, command, command, owner_id=user_id
        )
        await self._reply_screen(
            message,
            f"⚠️ Выполнить команду?\n<code>{ui.esc(command)}</code>",
            markup=ui.confirm_menu(ACTION_COMMAND, pending.token),
        )

    def _screen_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._screen_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._screen_locks[chat_id] = lock
        return lock

    async def _tracked_screen(self, chat_id: int) -> int:
        """Id текущего меню: сначала память, затем база после рестарта."""
        message_id = self._last_screen.get(chat_id, 0)
        if message_id:
            return message_id
        try:
            message_id = await asyncio.to_thread(
                self.storage.get_screen_message, chat_id
            )
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать прошлый экран: %s", exc)
            return 0
        if message_id:
            self._last_screen[chat_id] = message_id
        return message_id

    async def _remember_screen(self, chat_id: int, message_id: int) -> None:
        if not chat_id or not message_id:
            return
        self._last_screen[chat_id] = message_id
        try:
            await asyncio.to_thread(
                self.storage.set_screen_message, chat_id, message_id
            )
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить текущий экран: %s", exc)

    async def _edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if not chat_id or not message_id:
            return False
        try:
            await self.app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except BadRequest as exc:
            # Повторная отрисовка того же экрана: это успех, а не повод
            # отправить второе сообщение.
            if "not modified" in str(exc).lower():
                await self._remember_screen(chat_id, message_id)
                return True
            logger.debug("Не удалось обновить сообщение действия: %s", exc)
            return False
        except TelegramError as exc:
            logger.debug("Не удалось обновить сообщение действия: %s", exc)
            return False
        await self._remember_screen(chat_id, message_id)
        return True

    async def _show_screen(
        self,
        chat_id: int,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
        reply_to: Message | None = None,
    ) -> Message | None:
        """Показывает экран, оставляя в чате только одно сообщение бота."""
        if not chat_id:
            return None
        async with self._screen_lock(chat_id):
            previous = await self._tracked_screen(chat_id)
            sender = (
                reply_to.reply_text
                if reply_to is not None
                else self.app.bot.send_message
            )
            try:
                sent = await _send_with_retry(
                    sender,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            except TelegramError as exc:
                logger.warning("Не удалось отправить экран: %s", exc)
                return None
            if sent is None:
                return None
            mid = getattr(sent, "message_id", None)
            if not isinstance(mid, int):
                return cast(Message | None, sent)
            await self._remember_screen(chat_id, mid)
            if previous and previous != mid:
                bot = reply_to.get_bot() if reply_to is not None else self.app.bot
                try:
                    await bot.delete_message(chat_id, previous)
                except TelegramError as exc:
                    logger.debug("Не удалось удалить прошлый экран: %s", exc)
                    # Если удалить старый нельзя, возвращаем его на место:
                    # лучше одно сообщение с новым текстом, чем два меню.
                    with suppress(TelegramError):
                        await bot.delete_message(chat_id, mid)
                    if await self._edit_message(chat_id, previous, text, markup=markup):
                        return cast(Message | None, sent)
            return cast(Message | None, sent)

    async def _replace_or_notify(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Одно сообщение на действие: правим исходное, иначе шлём новое."""
        if await self._edit_message(chat_id, message_id, text, markup=markup):
            return True
        return bool(await self._notify(chat_id, text, markup=markup))

    async def _reply_screen(
        self,
        message: Message,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        """Показывает новый экран и убирает предыдущий, чтобы меню не копились."""
        return await self._show_screen(
            message.chat_id, text, markup=markup, reply_to=message
        )

    async def _show_run_log(
        self, callback: CallbackQuery, data: str, user_id: int
    ) -> None:
        """«Ещё 200 строк»/«Только ошибки» для логов из /run."""
        if not self.is_admin(user_id):
            await self._edit(callback, "⛔ Только для администраторов.")
            return
        kind, _, base = data.partition(":")
        variant = (LOG_MORE if kind == "logmore" else LOG_ERRORS).get(base)
        if variant is None or base not in ui.LOG_WITH_ERRORS:
            await self._edit(
                callback,
                "Неизвестный вариант журнала.",
                markup=self._main_menu(user_id),
            )
            return
        wait = self._heavy_wait(user_id)
        if wait > 0:
            await self._answer(callback, f"Журнал: следующий запрос через {wait:.0f} с")
            return
        await self._edit(
            callback, "📜 Читаю журнал…", markup=ui.log_variants_menu(base)
        )
        try:
            result = await self.actions.run_allowed_command(variant)
        except ActionError as exc:
            await self._edit(
                callback,
                f"❌ {ui.esc(str(exc))}",
                markup=ui.log_variants_menu(base),
            )
            return
        label = "только ошибки" if kind == "logerr" else "ещё строк"
        await self._edit(
            callback,
            f"📜 <b>{ui.esc(base)}</b> · {label}\n"
            f"<pre>{ui.esc(result.summary(2500))}</pre>",
            markup=ui.log_variants_menu(base),
        )

    async def _show_logs(
        self,
        callback: CallbackQuery,
        unit: str,
        *,
        lines: int = 40,
        errors: bool = False,
    ) -> None:
        if not self.is_admin(callback.from_user.id):
            await self._edit(callback, "⛔ Только для администраторов.")
            return
        wait = self._heavy_wait(callback.from_user.id)
        if wait > 0:
            await callback.answer(f"Журнал: следующий запрос через {wait:.0f} с")
            return
        await self._edit(
            callback,
            f"📜 Журнал <code>{ui.esc(unit)}</code>…",
            markup=ui.log_view_menu(unit),
        )
        try:
            output = await self.actions.journal(unit, lines, errors=errors)
        except ActionError as exc:
            await self._edit(
                callback,
                f"❌ {ui.esc(str(exc))}",
                markup=ui.log_view_menu(unit),
            )
            return
        where = "только ошибки" if errors else "последние строки"
        await self._edit(
            callback,
            f"📜 <b>{ui.esc(unit)}</b> · {where}\n<pre>{ui.esc(output[-2500:])}</pre>",
            markup=ui.log_view_menu(unit),
        )

    async def _adopt_screen(self, message: Message) -> None:
        """Делает сообщение, на кнопку которого нажали, единственным экраном."""
        chat_id = message.chat_id
        async with self._screen_lock(chat_id):
            previous = await self._tracked_screen(chat_id)
            if previous and previous != message.message_id:
                try:
                    await message.get_bot().delete_message(chat_id, previous)
                except TelegramError as exc:
                    # Своё сообщение обычно удаляется; если Telegram не дал,
                    # продолжаем с нажатым экраном, чтобы действие не сорвалось.
                    logger.debug("Не удалось удалить лишний экран: %s", exc)
            await self._remember_screen(chat_id, message.message_id)

    async def _edit(
        self,
        callback: CallbackQuery,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            screen = _message_of(callback)
        except RuntimeError:
            screen = None
        if screen is not None:
            await self._adopt_screen(screen)
        try:
            await _send_with_retry(
                callback.edit_message_text,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
            return
        except BadRequest as exc:
            # Повторное нажатие «Обновить» с теми же данными: содержимое не
            # изменилось — это норма, а не ошибка. Иначе мы бы продублировали
            # сообщение ответом ниже.
            if "not modified" in str(exc).lower():
                logger.debug("Сообщение не изменилось, обновление пропущено")
                return
            logger.warning("Не удалось обновить сообщение: %s", exc)
        except TelegramError as exc:
            logger.warning("Не удалось обновить сообщение: %s", exc)
        message = _message_of(callback)
        await self._show_screen(message.chat_id, text, markup=markup, reply_to=message)
