"""Step-up 2FA: разблокировка, TOTP, повторное использование, меню."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.constants import ChatType

from stacksentry import ui
from stacksentry.actions import (
    ACTION_JOURNAL,
    ACTION_REBOOT,
    ACTION_RESTART_SERVICE,
)
from stacksentry.app import MonitorBot
from stacksentry.settings import Settings
from stacksentry.totp import totp_code

ADMIN_ID = 42
TOTP_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _bot(make_settings: Callable[..., Settings], **overrides: object) -> MonitorBot:
    return MonitorBot(
        make_settings(
            require_2fa=True,
            totp_secret=TOTP_SECRET,
            **overrides,
        )
    )


async def _press(bot: MonitorBot, data: str) -> tuple[str, object]:
    telegram_bot = AsyncMock()
    query = CallbackQuery(
        id="1",
        from_user=User(id=ADMIN_ID, first_name="admin", is_bot=False),
        chat_instance="ci",
        data=data,
        inline_message_id="inline-1",
    )
    query.set_bot(telegram_bot)

    class Ctx:
        job_queue = None
        user_data: dict[str, object] = {}

    await bot.on_callback(Update(update_id=1, callback_query=query), Ctx())  # type: ignore[arg-type]
    call = telegram_bot.edit_message_text.await_args
    return call.kwargs["text"], call.kwargs.get("reply_markup")


def test_2fa_only_for_protected_actions(
    make_settings: Callable[..., Settings],
) -> None:
    bot = _bot(make_settings)
    try:
        assert bot._needs_2fa(ACTION_RESTART_SERVICE)
        assert bot._needs_2fa(ACTION_REBOOT)
        assert not bot._needs_2fa(ACTION_JOURNAL)
        assert not bot._is_elevated(ADMIN_ID)
    finally:
        bot.storage.close()


def test_elevation_and_fresh_window(
    make_settings: Callable[..., Settings],
) -> None:
    bot = _bot(make_settings)
    try:
        bot.storage.set_elevation(ADMIN_ID, 900)
        assert bot._is_elevated(ADMIN_ID)
        assert bot._is_elevated(ADMIN_ID, fresh=True)
        bot.storage.set_elevation(ADMIN_ID, 600)
        assert bot._is_elevated(ADMIN_ID)
        assert not bot._is_elevated(ADMIN_ID, fresh=True)
    finally:
        bot.storage.close()


def test_totp_replay_and_lockout(make_settings: Callable[..., Settings]) -> None:
    bot = _bot(make_settings)
    try:
        code = totp_code(TOTP_SECRET)
        ok, _reason, _left = bot._check_totp(ADMIN_ID, code)
        assert ok
        ok, reason, _left = bot._check_totp(ADMIN_ID, code)
        assert not ok and "уже использован" in reason
        for _ in range(3):
            bot._check_totp(99, "000000")
        assert bot._totp_lock_left(99) > 0
    finally:
        bot.storage.close()


async def test_dangerous_action_asks_for_totp(
    make_settings: Callable[..., Settings],
) -> None:
    bot = _bot(make_settings)
    try:
        text, markup = await _press(bot, "ask:restart_service:x-ui")
        assert "Нужен код 2FA" in text
        assert bot._pending_2fa[ADMIN_ID]["action"] == "restart_service"
        assert markup is not None
    finally:
        bot.storage.close()


async def test_wrong_code_deletes_message_and_keeps_one_2fa_screen(
    make_settings: Callable[..., Settings],
) -> None:
    bot = _bot(make_settings)
    try:
        bot._pending_2fa[ADMIN_ID] = {
            "action": "unlock",
            "arg": "",
            "data": "security",
            "expires": time.time() + 60,
        }
        bot._last_screen[1] = 10
        bot._check_totp = lambda *_args: (False, "Неверный код.", 2)  # type: ignore[method-assign]
        telegram_bot = AsyncMock()
        message = Message(
            message_id=2,
            date=datetime.now(tz=timezone.utc),
            chat=Chat(id=1, type=ChatType.PRIVATE),
            from_user=User(id=ADMIN_ID, first_name="admin", is_bot=False),
            text="000000",
        )
        message.set_bot(telegram_bot)

        class Ctx:
            job_queue = None
            user_data: dict[str, object] = {}

        await bot.on_text(
            Update(update_id=2, message=message),
            Ctx(),  # type: ignore[arg-type]
        )

        deleted = telegram_bot.delete_message.await_args
        assert deleted.kwargs["chat_id"] == 1
        assert deleted.kwargs["message_id"] == 2
        assert telegram_bot.send_message.await_count == 0
        edit = telegram_bot.edit_message_text.await_args
        assert edit.kwargs["message_id"] == 10
        assert "Неверный код. Проверьте время на устройстве." in edit.kwargs["text"]
    finally:
        bot.storage.close()


async def test_valid_code_edits_one_screen_and_does_not_send_new(
    make_settings: Callable[..., Settings],
) -> None:
    bot = _bot(make_settings)
    try:
        bot._pending_2fa[ADMIN_ID] = {
            "action": ACTION_RESTART_SERVICE,
            "arg": "x-ui",
            "data": "ask:restart_service:x-ui",
            "expires": time.time() + 60,
        }
        bot._last_screen[1] = 10
        telegram_bot = AsyncMock()
        message = Message(
            message_id=2,
            date=datetime.now(tz=timezone.utc),
            chat=Chat(id=1, type=ChatType.PRIVATE),
            from_user=User(id=ADMIN_ID, first_name="admin", is_bot=False),
            text=totp_code(TOTP_SECRET),
        )
        message.set_bot(telegram_bot)

        class Ctx:
            job_queue = None
            user_data: dict[str, object] = {}

        await bot.on_text(
            Update(update_id=2, message=message),
            Ctx(),  # type: ignore[arg-type]
        )

        assert telegram_bot.send_message.await_count == 0
        assert telegram_bot.delete_message.await_count == 1
        edits = telegram_bot.edit_message_text.await_args_list
        assert edits
        assert all(call.kwargs["message_id"] == 10 for call in edits)
        texts = [call.kwargs["text"] for call in edits]
        assert any("разблокированы" in text for text in texts)
        assert any("Перезапустить сервис" in text for text in texts)
    finally:
        bot.storage.close()


async def test_code_unlocks_and_shows_confirmation(
    make_settings: Callable[..., Settings],
) -> None:
    bot = _bot(make_settings)
    try:
        await _press(bot, "ask:restart_service:x-ui")
        telegram_bot = AsyncMock()
        message = Message(
            message_id=2,
            date=datetime.now(tz=timezone.utc),
            chat=Chat(id=1, type=ChatType.PRIVATE),
            from_user=User(id=ADMIN_ID, first_name="admin", is_bot=False),
            text=totp_code(TOTP_SECRET),
        )
        message.set_bot(telegram_bot)
        update = Update(update_id=2, message=message)

        class Ctx:
            job_queue = None
            user_data: dict[str, object] = {}

        await bot.on_text(update, Ctx())  # type: ignore[arg-type]

        assert bot.storage.elevation_until(ADMIN_ID) > 0
        assert ADMIN_ID not in bot._pending_2fa
        texts = [
            call.kwargs.get("text", "")
            for call in telegram_bot.send_message.await_args_list
        ]
        assert any("разблокированы" in text for text in texts)
        assert any("Перезапустить сервис" in text for text in texts)
    finally:
        bot.storage.close()


def test_security_ui_and_menu(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(require_2fa=True, totp_secret=TOTP_SECRET)
    text = ui.render_security(settings, 0.0)
    assert "2FA" in text and "Заблокировано" in text
    data = {
        button.callback_data
        for row in ui.security_menu(False).inline_keyboard
        for button in row
    }
    assert "security:unlock" in data
    assert "help" in {
        button.callback_data
        for row in ui.main_menu(True).inline_keyboard
        for button in row
    }
    assert "security" in {
        button.callback_data
        for row in ui.main_menu(True).inline_keyboard
        for button in row
    }
