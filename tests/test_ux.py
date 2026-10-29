"""UX и целостность меню: каждая кнопка работает и не оставляет тупиков.

Эти тесты ловят класс ошибок, который не виден в юнит-тестах логики: кнопка
нарисована, но её callback никто не обработал; экран открылся без клавиатуры,
и вернуться в меню нечем.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from telegram.constants import ChatType

from stacksentry import ui
from stacksentry.app import MonitorBot
from stacksentry.settings import BASE_DIR, ConfigError, Settings, load_settings

ADMIN_ID = 42
_SRC_DIR = BASE_DIR / "stacksentry"
UI_SRC = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(_SRC_DIR.glob("ui*.py"))
)
APP_SRC = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(_SRC_DIR.glob("app*.py"))
)


# ---------------------------------------------------- статическая проверка


def _all_callback_data() -> set[str]:
    """Все callback_data, которые может нарисовать ui.py (f-строки — с образцом)."""
    found: set[str] = set(re.findall(r'callback_data="([^"]+)"', UI_SRC))
    for template in re.findall(r'callback_data=f"([^"]+)"', UI_SRC):
        found.add(re.sub(r"\{[^}]*\}", "SAMPLE", template))
    return found


def test_every_button_callback_is_handled() -> None:
    """Кнопка без обработчика = «Неизвестная кнопка» у пользователя."""
    data = _all_callback_data()
    assert len(data) >= 10, data
    for item in sorted(data):
        prefix = item.split(":", 1)[0]
        handled = (
            f'data == "{item}"' in APP_SRC
            or f'startswith("{prefix}:")' in APP_SRC
            or f'data == "{prefix}"' in APP_SRC
            # Литералы, сгруппированные в набор: data in {"details", "server"}.
            or f'"{item}"' in APP_SRC
        )
        assert handled, f"кнопка {item!r} нигде не обрабатывается"


def test_every_confirm_button_has_description() -> None:
    """Кнопка подтверждения без описания = «Неизвестное действие» у админа."""
    from stacksentry.app import CONFIRM_DESCRIPTIONS

    for data in sorted(_all_callback_data()):
        if not data.startswith("ask:"):
            continue
        action = data[4:].split(":", 1)[0]
        assert action in CONFIRM_DESCRIPTIONS, action


def test_callback_data_fits_telegram_limit() -> None:
    """Лимит Telegram — 64 байта; длинное имя юнита не должно ломать меню."""
    for markup in (
        ui.main_menu(True),
        ui.services_menu(("x-ui", "fail2ban"), True),
        ui.logs_menu(("x-ui",)),
        ui.maintenance_menu(),
        ui.confirm_menu("restart_service", "abcdefgh"),
        ui.subscriptions_menu(10, has_more=True),
        ui.result_menu(),
    ):
        for row in markup.inline_keyboard:
            for button in row:
                assert isinstance(button.callback_data, str)
                assert len(button.callback_data.encode()) <= 64, button.callback_data


# ----------------------------------------------------- живое нажатие кнопок


@pytest.fixture
async def bot_app(make_settings: Callable[..., Settings]) -> AsyncIterator[MonitorBot]:
    # Порт закрыт: сеть не нужна, панель мгновенно считается недоступной.
    instance = MonitorBot(
        make_settings(
            admin_ids=(ADMIN_ID,),
            panel_url="http://127.0.0.1:1",
            panel_timeout=0.5,
        )
    )
    try:
        yield instance
    finally:
        await instance.monitor.close()
        instance.storage.close()


async def _press_all(
    bot_app: MonitorBot, data: str
) -> list[tuple[str, InlineKeyboardMarkup | None]]:
    """Все правки сообщения: видно и промежуточный «⏳», и финальный экран."""
    telegram_bot = AsyncMock()
    query = CallbackQuery(
        id="1",
        from_user=User(id=ADMIN_ID, first_name="admin", is_bot=False),
        chat_instance="ci",
        data=data,
        inline_message_id="inline-1",
    )
    query.set_bot(telegram_bot)
    await bot_app.on_callback(Update(update_id=1, callback_query=query), _Context())  # type: ignore[arg-type]
    return [
        (call.kwargs["text"], call.kwargs.get("reply_markup"))
        for call in telegram_bot.edit_message_text.await_args_list
    ]


async def _press(
    bot_app: MonitorBot, data: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    calls = await _press_all(bot_app, data)
    return calls[-1]


@pytest.mark.parametrize(
    "data",
    [
        "refresh",
        "home",
        "server",
        "services",
        "panel",
        "traffic",
        "subs",
        "subs:10",
        "history",
        "logs_menu",
        "maintenance",
        "command_prompt",
        "details",
        "help",
        "audit:denied",
        "audit:all",
        "mute:SAMPLE",
        "no:abcdefgh",
        "ask:restart_service:x-ui",
        "ask:restart_xray:",
        "ask:apt_upgrade:",
        "ask:dist_upgrade:",
    ],
)
async def test_button_press_never_strands_the_user(
    bot_app: MonitorBot, data: str
) -> None:
    """После любой кнопки есть текст и клавиатура: пользователь не в тупике."""
    text, markup = await _press(bot_app, data)
    assert text
    assert "Неизвестная кнопка" not in text, text
    assert markup is not None, f"кнопка {data!r} оставила экран без навигации"


async def test_button_press_shows_immediate_progress(bot_app: MonitorBot) -> None:
    """Без промежуточного «⏳» медленная панель выглядит как зависший бот."""
    calls = await _press_all(bot_app, "panel")
    assert any("Собираю данные" in text for text, _ in calls)


async def test_cancelled_action_returns_to_menu(bot_app: MonitorBot) -> None:
    pending = bot_app.actions.request("reboot", "перезагрузка", owner_id=ADMIN_ID)
    text, markup = await _press(bot_app, f"no:{pending.token}")
    assert "Отменено" in text
    assert markup is not None
    assert markup.inline_keyboard, "после отмены клавиатура не должна исчезать"


async def test_cancel_of_unknown_token_is_reported_honestly(
    bot_app: MonitorBot,
) -> None:
    """Раньше бот писал «Отменено», даже когда отменять было нечего."""
    text, _ = await _press(bot_app, "no:unknown123")
    assert "Отменено" not in text
    assert "неактуально" in text


# -------------------------------------------------------------- прочее UX


@pytest.mark.parametrize(
    ("user_id", "expect_keyboard"),
    [(999, False), (ADMIN_ID, True)],
    ids=["гость", "админ"],
)
async def test_start_keyboard_matches_rights(
    make_settings: Callable[..., Settings],
    user_id: int,
    expect_keyboard: bool,
) -> None:
    """Кнопки в /start показываем только тому, кто может ими пользоваться."""
    instance = MonitorBot(make_settings(admin_ids=(ADMIN_ID,)))
    try:
        telegram_bot = AsyncMock()
        message = Message(
            message_id=1,
            date=datetime.now(tz=timezone.utc),
            chat=Chat(id=999, type=ChatType.PRIVATE),
            from_user=User(id=user_id, first_name="u", is_bot=False),
            text="/start",
        )
        message.set_bot(telegram_bot)
        update = Update(update_id=1, message=message)

        await instance.cmd_start(update, _Context())  # type: ignore[arg-type]

        assert telegram_bot.send_message.await_count == 1
        markup = telegram_bot.send_message.await_args.kwargs.get("reply_markup")
        assert (markup is not None) is expect_keyboard
    finally:
        instance.storage.close()


def _message_update(
    telegram_bot: AsyncMock, *, chat_type: ChatType, user_id: int, text: str = "/status"
) -> Update:
    message = Message(
        message_id=1,
        date=datetime.now(tz=timezone.utc),
        chat=Chat(id=-100 if chat_type != ChatType.PRIVATE else 1, type=chat_type),
        from_user=User(id=user_id, first_name="u", is_bot=False),
        text=text,
    )
    message.set_bot(telegram_bot)
    return Update(update_id=1, message=message)


async def test_commands_in_group_chat_are_refused(bot_app: MonitorBot) -> None:
    """Иначе /status выложит адреса, подписки и трафик прямо в групповой чат."""
    telegram_bot = AsyncMock()
    update = _message_update(telegram_bot, chat_type=ChatType.GROUP, user_id=ADMIN_ID)

    await bot_app.cmd_status(update, _Context())  # type: ignore[arg-type]

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "личном чате" in text
    assert "Состояние сервера" not in text


async def test_commands_in_private_chat_work(bot_app: MonitorBot) -> None:
    telegram_bot = AsyncMock()
    update = _message_update(telegram_bot, chat_type=ChatType.PRIVATE, user_id=ADMIN_ID)

    await bot_app.cmd_status(update, _Context())  # type: ignore[arg-type]

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "Состояние сервера" in text


async def test_guest_denial_is_written_to_audit(bot_app: MonitorBot) -> None:
    """Попытки доступа не-администратора должны оставаться в журнале."""
    telegram_bot = AsyncMock()
    update = _message_update(
        telegram_bot, chat_type=ChatType.PRIVATE, user_id=999, text="/status"
    )

    await bot_app.cmd_status(update, _Context())  # type: ignore[arg-type]

    rows = bot_app.storage.recent_audit(1)
    assert rows[0]["action"] == "denied"
    assert rows[0]["telegram_id"] == 999


def test_heavy_commands_are_rate_limited(bot_app: MonitorBot) -> None:
    """Журнал читается через sudo — не даём запускать его подряд."""
    assert bot_app._heavy_wait(ADMIN_ID) == 0.0
    wait = bot_app._heavy_wait(ADMIN_ID)
    assert wait > 0.0


# ---------------------------------------------------------- нарезка HTML


def test_split_keeps_html_tags_whole() -> None:
    """Нарезка длинных сообщений не должна рвать теги Telegram."""
    from stacksentry.app import _split

    lines = [f"<b>Строка {i}</b> — значение {i}" for i in range(400)]
    text = "\n".join(lines)
    chunks = _split(text, 500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)

    # Видимый текст сохраняется; на границах теги закрываются и открываются.
    def strip(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value)

    assert strip("".join(chunks)) == strip(text)
    for chunk in chunks:
        # Каждая часть самодостаточна по разметке: открытые теги закрыты.
        assert chunk.count("<b>") == chunk.count("</b>"), chunk[:80]


def test_split_short_text_is_untouched() -> None:
    from stacksentry.app import _split

    assert _split("короткий <b>текст</b>", 100) == ["короткий <b>текст</b>"]


# ------------------------------------------------------- валидация SERVICES


def test_services_with_unsafe_name_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com\n"
        "PANEL_API_TOKEN=token\n"
        "SERVICES=x-ui,ssh -i key\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="SERVICES"):
        load_settings(path)


def test_services_too_long_for_button_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com\n"
        "PANEL_API_TOKEN=token\n"
        f"SERVICES={'a' * 60}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="слишком длинное"):
        load_settings(path)


class _Context:
    def __init__(self) -> None:
        self.job_queue = None
        self.user_data: dict[str, object] | None = None


def test_help_manual_explains_metrics() -> None:
    """Инструкция внутри бота должна объяснять метрики, а не только команды."""
    text = ui.render_help(True)

    for marker in (
        "Сервисы",
        "Панель",
        "Подписки",
        "Что значат цифры",
        "stacksentry-menu",
        "Mini App",
        "опционально",
        "QR",
    ):
        assert marker in text, marker
    assert "/services" in text
    assert len(text) < 4000, "помощь должна укладываться в одно сообщение Telegram"


def test_help_button_is_in_main_menu() -> None:
    data = [b.callback_data for row in ui.main_menu(True).inline_keyboard for b in row]
    assert "help" in data


def test_maintenance_has_separate_apt_update_and_release() -> None:
    """apt update, проверка релиза и запуск do-release-upgrade — разные кнопки."""
    data = {
        button.callback_data
        for row in ui.maintenance_menu().inline_keyboard
        for button in row
    }
    assert "update_check" in data
    assert "ask:apt_update:" in data
    assert "ask:apt_upgrade:" in data
    assert "ask:apt_upgrade_phased:" in data
    assert "ask:dist_upgrade:" not in data  # проверка входит в запуск релиза
    assert "ask:release_upgrade:" in data


def test_help_is_a_full_presentational_manual() -> None:
    text = ui.render_help(True)
    for marker in (
        "С чего начать",
        "Разделы",
        "Обслуживание",
        "apt update",
        "do-release-upgrade",
        "Команда на сервер",
        "Что значат цифры",
        "Безопасность",
        "Настройки",
        "Если что-то не работает",
    ):
        assert marker in text, marker
    assert 2000 < len(text) < 4000


def test_apt_status_header_is_parsed() -> None:
    from stacksentry.app import _parse_apt_status

    fields, tail = _parse_apt_status(
        "STATE=inactive\nRESULT=success\nEXIT=0\nLOAD=loaded\n"
        "PACKAGES=2\nPACKAGE_LIST=curl bash\n"
        "Reading package lists...\nDone"
    )
    assert fields == {
        "STATE": "inactive",
        "RESULT": "success",
        "EXIT": "0",
        "LOAD": "loaded",
        "PACKAGES": "2",
        "PACKAGE_LIST": "curl bash",
    }
    assert tail == "Reading package lists...\nDone"


async def test_run_command_works_from_any_screen(bot_app: MonitorBot) -> None:
    """Команду из allowlist можно прислать в любом разделе — без /maint."""
    telegram_bot = AsyncMock()
    update = _message_update(
        telegram_bot, chat_type=ChatType.PRIVATE, user_id=ADMIN_ID, text="/run free -h"
    )
    context = _Context()
    context.user_data = {}

    await bot_app.cmd_run(update, context)  # type: ignore[arg-type]

    sent = telegram_bot.send_message.await_args
    assert "Выполнить команду" in sent.kwargs["text"]
    markup = sent.kwargs["reply_markup"]
    assert markup is not None
    assert any(
        "free -h" not in (b.callback_data or "") and "do:" in (b.callback_data or "")
        for row in markup.inline_keyboard
        for b in row
    )


async def test_run_rejects_command_outside_allowlist(bot_app: MonitorBot) -> None:
    telegram_bot = AsyncMock()
    update = _message_update(
        telegram_bot, chat_type=ChatType.PRIVATE, user_id=ADMIN_ID, text="/run rm -rf /"
    )
    context = _Context()
    context.user_data = {}

    await bot_app.cmd_run(update, context)  # type: ignore[arg-type]

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "не в белом списке" in text


async def test_previous_screen_is_deleted(bot_app: MonitorBot) -> None:
    """Переход между меню заменяет прошлый экран, а не копит вереницу."""
    bot = AsyncMock()

    class FakeMessage:
        chat_id = 7

        def __init__(self) -> None:
            self.calls = 0

        def get_bot(self) -> AsyncMock:
            return bot

        async def reply_text(self, **_kwargs: object) -> object:
            self.calls += 1
            return SimpleNamespace(message_id=100 + self.calls)

    message = FakeMessage()
    bot_app._last_screen[7] = 50

    first = await bot_app._reply_screen(message, "первый")  # type: ignore[arg-type]
    second = await bot_app._reply_screen(message, "второй")  # type: ignore[arg-type]

    assert first is not None and first.message_id == 101
    assert second is not None and second.message_id == 102
    # Каждый новый экран убирает предыдущий: 50 → 101, затем 101 → 102.
    assert [c.args for c in bot.delete_message.await_args_list] == [(7, 50), (7, 101)]
    assert bot_app._last_screen[7] == 102
    assert bot_app.storage.get_screen_message(7) == 102


async def test_screen_survives_restart(bot_app: MonitorBot) -> None:
    """После рестарта бот помнит прошлое меню и не оставляет его сиротой."""
    bot = AsyncMock()

    class FakeMessage:
        chat_id = 7

        def get_bot(self) -> AsyncMock:
            return bot

        async def reply_text(self, **_kwargs: object) -> object:
            return SimpleNamespace(message_id=101)

    bot_app._last_screen.clear()
    bot_app.storage.set_screen_message(7, 50)

    sent = await bot_app._reply_screen(FakeMessage(), "после рестарта")  # type: ignore[arg-type]

    assert sent is not None and sent.message_id == 101
    assert [c.args for c in bot.delete_message.await_args_list] == [(7, 50)]
    assert bot_app._last_screen[7] == 101


async def test_concurrent_screens_leave_one(bot_app: MonitorBot) -> None:
    """Два одновременных ответа не должны оставить два меню."""
    bot = AsyncMock()
    counter = 0

    class FakeMessage:
        chat_id = 7

        def get_bot(self) -> AsyncMock:
            return bot

        async def reply_text(self, **_kwargs: object) -> object:
            nonlocal counter
            counter += 1
            return SimpleNamespace(message_id=100 + counter)

    bot_app._last_screen.clear()
    message = FakeMessage()

    await asyncio.gather(
        bot_app._reply_screen(message, "первый"),  # type: ignore[arg-type]
        bot_app._reply_screen(message, "второй"),  # type: ignore[arg-type]
    )

    assert counter == 2
    assert [c.args for c in bot.delete_message.await_args_list] == [(7, 101)]
    assert bot_app._last_screen[7] == 102


async def test_stale_message_is_not_second_menu(bot_app: MonitorBot) -> None:
    """Кнопка на старом меню делает его текущим, а лишнее меню убирает."""
    bot = AsyncMock()

    class FakeMessage:
        chat_id = 7
        message_id = 200

        def get_bot(self) -> AsyncMock:
            return bot

    bot_app._last_screen[7] = 100

    await bot_app._adopt_screen(FakeMessage())  # type: ignore[arg-type]

    assert [c.args for c in bot.delete_message.await_args_list] == [(7, 100)]
    assert bot_app._last_screen[7] == 200


async def test_private_notify_replaces_screen(bot_app: MonitorBot) -> None:
    """Алерт в личном чате обновляет меню, а не добавляет сообщение."""
    calls: list[tuple[int, str]] = []

    async def fake_show(chat_id: int, text: str, **_kwargs: object) -> object:
        calls.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    bot_app._show_screen = fake_show  # type: ignore[assignment]

    assert await bot_app._notify(ADMIN_ID, "алерт")
    assert calls == [(ADMIN_ID, "алерт")]


async def test_stale_callback_answer_does_not_crash(bot_app: MonitorBot) -> None:
    """Telegram отвечает «query is too old» — это не должно ронять on_callback."""
    from telegram.error import BadRequest

    class StaleQuery:
        async def answer(self, **_kwargs: object) -> None:
            raise BadRequest("query is too old and response timeout expired")

    await bot_app._answer(StaleQuery())  # type: ignore[arg-type]


def test_command_help_is_grouped_and_described() -> None:
    """Список /run — человеческий: группы и описания, а не голые алиасы."""
    from stacksentry.settings import BASE_DIR

    line = next(
        item
        for item in (BASE_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
        if item.startswith("COMMAND_ALLOWLIST=")
    )
    commands = tuple(
        item.strip() for item in line.split("=", 1)[1].split(",") if item.strip()
    )
    text = ui.render_command_help(commands)

    for group in ("Логи", "Сеть", "Службы", "Система", "Обновления", "Ресурсы"):
        assert f"<b>{group}</b>" in text, group
    assert "журнал ОС" in text
    assert "слушающие" in text
    assert "пакеты, доступные к обновлению" in text
    assert 1000 < len(text) < 4000


def test_command_help_shows_only_allowed() -> None:
    text = ui.render_command_help(("os_logs",))
    assert "os_logs" in text
    assert "sockets" not in text
    assert "Логи" in text
    assert "Сеть" not in text


def test_log_variants_menu_has_more_and_errors() -> None:
    data = {
        button.callback_data
        for row in ui.log_variants_menu("os_logs").inline_keyboard
        for button in row
    }
    assert "logmore:os_logs" in data
    assert "logerr:os_logs" in data
    # У dmesg нет «ещё строк», только ошибки.
    dmesg = {
        button.callback_data
        for row in ui.log_variants_menu("dmesg").inline_keyboard
        for button in row
    }
    assert "logerr:dmesg" in dmesg
    assert "logmore:dmesg" not in dmesg
