"""Тесты сборки Telegram-приложения и прав доступа.

Сборка Application проверяется отдельно от сети: именно здесь однажды
оказался несуществующий метод билдера, и бот падал на старте с
AttributeError, не доходя до сети.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Update, User
from telegram.ext import CommandHandler

from stacksentry.app import MonitorBot
from stacksentry.settings import Settings
from stacksentry.update_spool import (
    read_status,
    request_path,
    write_json,
    write_status,
)
from stacksentry.updates import InstallInfo, UpdateInfo

ADMIN_ID = 42
GUEST_ID = 999


@pytest.fixture
async def bot_app(make_settings: Callable[..., Settings]) -> AsyncIterator[MonitorBot]:
    instance = MonitorBot(make_settings(admin_ids=(ADMIN_ID,)))
    try:
        yield instance
    finally:
        await instance.monitor.close()
        instance.storage.close()


def test_application_is_built_and_handlers_registered(
    bot_app: MonitorBot,
) -> None:
    app = bot_app.app
    assert app.bot_data["bot"] is bot_app

    commands = {
        command
        for handlers in app.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {
        "start",
        "help",
        "status",
        "services",
        "panel",
        "subs",
        "history",
        "logs",
        "audit",
        "maint",
        "version",
        "updatecheck",
    } <= commands


@pytest.mark.parametrize(
    "data",
    [
        "refresh",
        "server",
        "services",
        "panel",
        "traffic",
        "subs",
        "history",
        "maintenance",
        "logs_menu",
        "logs:x-ui",
        "command_prompt",
        "ask:apt_upgrade:",
        "update_check",
        "no:abc",
    ],
)
async def test_guest_cannot_use_any_button(bot_app: MonitorBot, data: str) -> None:
    """Гость не должен получать данные сервера ни через одну кнопку."""
    telegram_bot = AsyncMock()
    query = CallbackQuery(
        id="1",
        from_user=User(id=GUEST_ID, first_name="guest", is_bot=False),
        chat_instance="ci",
        data=data,
        # Редактируем сообщение инлайн-кнопки: у CallbackQuery без message
        # иначе нет объекта, который можно править.
        inline_message_id="inline-1",
    )
    # В PTB бот в объект апдейта подставляет Application, в тестах — вручную.
    query.set_bot(telegram_bot)
    update = Update(update_id=1, callback_query=query)

    await bot_app.on_callback(update, _Context())  # type: ignore[arg-type]

    assert telegram_bot.edit_message_text.await_count == 1
    text = telegram_bot.edit_message_text.await_args.kwargs["text"]
    assert "Только для администраторов" in text


def test_is_admin_matches_configuration(bot_app: MonitorBot) -> None:
    assert bot_app.is_admin(ADMIN_ID) is True
    assert bot_app.is_admin(GUEST_ID) is False
    assert bot_app.is_admin(None) is False


async def test_custom_telegram_api_url_reaches_ptb_builder(
    make_settings: Callable[..., Settings],
) -> None:
    """Свой Bot API: URL из конфигурации должен дойти до клиента PTB."""
    instance = MonitorBot(make_settings(telegram_api_url="http://127.0.0.1:8081"))
    try:
        base_url = instance.app.bot.base_url
        assert base_url.startswith("http://127.0.0.1:8081/bot")
        assert base_url.endswith("123456789:AAconftest")
    finally:
        await instance.monitor.close()
        instance.storage.close()


class _Context:
    """Заглушка Context для вызовов, которым очередь задач не нужна."""

    def __init__(self) -> None:
        self.job_queue = None
        self.user_data: dict[str, object] | None = None


async def test_apt_watcher_reports_finished_job(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт об apt приходит в чат сам — без journalctl и лишних команд."""
    from stacksentry.models import CommandResult

    job_id = bot_app.storage.add_pending_job(
        "apt_upgrade", "upgrade", chat_id=1, user_id=ADMIN_ID
    )

    async def fake_status(_action: str) -> CommandResult:
        return CommandResult(
            action="apt_status",
            command="apt_status upgrade",
            returncode=0,
            stdout=(
                "STATE=inactive\nRESULT=success\nEXIT=0\nLOAD=loaded\n"
                "Setting up x-ui...\nDone"
            ),
            stderr="",
            duration=0.1,
        )

    notified: list[tuple[int, str]] = []

    async def fake_notify(chat_id: int, text: str, **_kwargs: object) -> bool:
        notified.append((chat_id, text))
        return True

    monkeypatch.setattr(bot_app.actions, "apt_status", fake_status)
    monkeypatch.setattr(bot_app, "_notify", fake_notify)

    class Job:
        data = {
            "job_id": job_id,
            "action": "apt_upgrade",
            "chat_id": 1,
            "user_id": ADMIN_ID,
            "created_at": 0.0,
            "attempts": 0,
            "errors": 0,
        }

    class Ctx:
        job = Job()

    await bot_app._job_watch_apt(Ctx())  # type: ignore[arg-type]

    assert notified
    assert "Обновление системы завершено" in notified[0][1]
    assert "Setting up x-ui" in notified[0][1]
    assert bot_app.storage.list_pending_jobs() == []


async def test_alert_outbox_retries_and_is_deduplicated(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один алерт не дублируется, а после сбоя остаётся в очереди."""
    await asyncio.to_thread(
        bot_app.storage.set_alert_state, "cpu", "critical", changed_at=100.0
    )
    first = bot_app.storage.enqueue_alert(
        state_key="cpu",
        rule="cpu",
        severity="critical",
        title="CPU",
        detail="96%",
    )
    duplicate = bot_app.storage.enqueue_alert(
        state_key="cpu",
        rule="cpu",
        severity="critical",
        title="CPU",
        detail="96%",
    )
    assert first > 0
    assert duplicate == 0
    assert len(bot_app.storage.pending_alerts()) == 1

    async def fail_notify(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(bot_app, "_notify", fail_notify)
    await bot_app._flush_alert_outbox()
    assert len(bot_app.storage.pending_alerts(now=time.time() + 60)) == 1

    async def ok_notify(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(bot_app, "_notify", ok_notify)
    await bot_app._flush_alert_outbox()
    assert bot_app.storage.pending_alerts() == []


async def test_alert_routes_by_severity_and_topic(bot_app: MonitorBot) -> None:
    bot_app.settings = replace(
        bot_app.settings,
        alert_critical_chat_id=-1001,
        alert_critical_topic_id=7,
        alert_warning_chat_id=-1002,
        alert_warning_topic_id=8,
    )

    assert await bot_app._alert_targets("critical") == [(-1001, 7)]
    assert await bot_app._alert_targets("warning") == [(-1002, 8)]
    assert await bot_app._alert_targets("ok") == [(-1002, 8)]


def test_quiet_hours_wrap_midnight(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot_app.settings = replace(
        bot_app.settings,
        alert_quiet_start="23:00",
        alert_quiet_end="08:00",
    )

    monkeypatch.setattr(
        "stacksentry.app_jobs.time.localtime",
        lambda: SimpleNamespace(tm_hour=23, tm_min=30),
    )
    assert bot_app._quiet_hours() is True

    monkeypatch.setattr(
        "stacksentry.app_jobs.time.localtime",
        lambda: SimpleNamespace(tm_hour=12, tm_min=0),
    )
    assert bot_app._quiet_hours() is False


async def test_bot_receives_web_update_request(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = "abcDEF1234567890"
    spool = bot_app.settings.update_spool_dir
    write_json(
        request_path(spool, request_id),
        {
            "id": request_id,
            "user_id": ADMIN_ID,
            "created_at": time.time(),
            "current_version": "1.0.0",
            "target_version": "1.1.0",
            "target_sha": "b" * 40,
        },
    )
    sent: list[tuple[int, str]] = []

    async def fake_notify(chat_id: int, text: str, **_kwargs: object) -> bool:
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(bot_app, "_notify", fake_notify)

    await bot_app.job_update_requests(None)  # type: ignore[arg-type]
    await bot_app.job_update_requests(None)  # type: ignore[arg-type]

    assert len(sent) == 1
    assert not request_path(spool, request_id).exists()
    status = read_status(spool, request_id)
    assert status is not None
    assert status["state"] == "awaiting_confirmation"
    assert status["progress"] == 20


async def test_web_update_finishes_after_restart(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = "finishABC12345678"
    spool = bot_app.settings.update_spool_dir
    write_status(
        spool,
        request_id,
        state="installing",
        progress=80,
        stage="Перезапускаю бота",
        target_sha="b" * 40,
        target_version="1.1.0",
    )
    monkeypatch.setattr(
        "stacksentry.app_jobs.read_install_info",
        lambda: InstallInfo(
            version="1.1.0",
            ref="main",
            sha="b" * 40,
            installed_at="",
            repo="Hijackery/stacksentry",
        ),
    )

    await bot_app._finish_web_updates()

    status = read_status(spool, request_id)
    assert status is not None
    assert status["state"] == "done"
    assert status["progress"] == 100


async def test_web_update_cancel_is_recorded(bot_app: MonitorBot) -> None:
    request_id = "cancelABC12345678"
    spool = bot_app.settings.update_spool_dir
    write_status(
        spool,
        request_id,
        state="awaiting_confirmation",
        progress=20,
        stage="Ожидает подтверждения",
    )

    assert await bot_app._cancel_web_update(request_id)
    status = read_status(spool, request_id)
    assert status is not None
    assert status["state"] == "cancelled"


async def test_update_notification_is_sent_once_per_commit(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новый commit в main анонсируется один раз, повторный цикл молчит."""
    info = UpdateInfo(
        current_version="1.0.0",
        current_sha="a" * 40,
        remote_sha="b" * 40,
        remote_version="1.1.0",
        remote_date="29.10.2026 10:00 UTC",
        remote_message="Новая версия",
        update_available=True,
        repo="Hijackery/stacksentry",
    )
    notified: list[tuple[int, str]] = []

    async def fake_notify(chat_id: int, text: str, **_kwargs: object) -> bool:
        notified.append((chat_id, text))
        return True

    monkeypatch.setattr("stacksentry.app_jobs.check_for_update", lambda: info)
    monkeypatch.setattr(bot_app, "_notify", fake_notify)

    await bot_app.job_update_check(None)  # type: ignore[arg-type]
    await bot_app.job_update_check(None)  # type: ignore[arg-type]

    assert len(notified) == 1
    assert notified[0][0] == ADMIN_ID
    assert "Доступно обновление" in notified[0][1]
    assert bot_app.storage.get_meta("update_notified_sha") == info.remote_sha


async def test_apt_watcher_shows_upgradable_packages(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apt update: список пакетов должен быть в начале отчёта, а не в хвосте лога."""
    from stacksentry.models import CommandResult

    job_id = bot_app.storage.add_pending_job(
        "apt_update", "update", chat_id=1, user_id=ADMIN_ID
    )

    async def fake_status(_action: str) -> CommandResult:
        return CommandResult(
            action="apt_status",
            command="apt_status update",
            returncode=0,
            stdout=(
                "STATE=inactive\nRESULT=success\nEXIT=0\nLOAD=loaded\n"
                "PACKAGES=2\nPACKAGE_LIST=docker-compose-plugin ghostscript\n"
                "Starting stacksentry-apt-update.service...\n"
                "apt: обновление индексов завершено (код 0)"
            ),
            stderr="",
            duration=0.1,
        )

    notified: list[tuple[int, str]] = []

    async def fake_notify(chat_id: int, text: str, **_kwargs: object) -> bool:
        notified.append((chat_id, text))
        return True

    monkeypatch.setattr(bot_app.actions, "apt_status", fake_status)
    monkeypatch.setattr(bot_app, "_notify", fake_notify)

    class Job:
        data = {
            "job_id": job_id,
            "action": "apt_update",
            "chat_id": 1,
            "user_id": ADMIN_ID,
            "created_at": 0.0,
            "attempts": 0,
            "errors": 0,
        }

    class Ctx:
        job = Job()

    await bot_app._job_watch_apt(Ctx())  # type: ignore[arg-type]

    assert notified
    report = notified[0][1]
    assert "К обновлению (2)" in report
    assert "docker-compose-plugin ghostscript" in report
    assert bot_app.storage.list_pending_jobs() == []


async def test_apt_watcher_keeps_polling_while_active(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stacksentry.models import CommandResult

    job_id = bot_app.storage.add_pending_job(
        "apt_upgrade", "upgrade", chat_id=1, user_id=ADMIN_ID
    )

    async def fake_status(_action: str) -> CommandResult:
        return CommandResult(
            action="apt_status",
            command="apt_status upgrade",
            returncode=0,
            stdout="STATE=active\nRESULT=unknown\nEXIT=unknown\nLOAD=loaded\n",
            stderr="",
            duration=0.1,
        )

    scheduled: list[int] = []
    monkeypatch.setattr(bot_app.actions, "apt_status", fake_status)
    monkeypatch.setattr(
        bot_app, "_schedule_apt_watch", lambda jid, *_a, **_k: scheduled.append(jid)
    )

    class Job:
        data = {
            "job_id": job_id,
            "action": "apt_upgrade",
            "chat_id": 1,
            "user_id": ADMIN_ID,
            "created_at": __import__("time").time(),
            "attempts": 0,
            "errors": 0,
        }

    class Ctx:
        job = Job()

    await bot_app._job_watch_apt(Ctx())  # type: ignore[arg-type]

    assert scheduled == [job_id]
    assert len(bot_app.storage.list_pending_jobs()) == 1


async def test_post_init_sets_webapp_menu_button(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка «Панель» у значка прикрепления — это chat menu button."""
    from dataclasses import replace
    from unittest.mock import AsyncMock

    bot_app.settings = replace(
        bot_app.settings,
        webapp_enabled=True,
        webapp_secret="x" * 32,
        webapp_public_origin="https://sentry.example.com",
    )
    bot_app._verify_after_reboot = AsyncMock()  # type: ignore[method-assign]
    bot_app._resume_apt_watches = AsyncMock()  # type: ignore[method-assign]
    setter = AsyncMock()
    monkeypatch.setattr(type(bot_app.app.bot), "set_chat_menu_button", setter)

    await bot_app._post_init(bot_app.app)

    setter.assert_awaited_once()
    assert setter.await_args is not None
    menu = setter.await_args.kwargs["menu_button"]
    assert menu.web_app.url == "https://sentry.example.com"


async def test_post_init_skips_webapp_menu_button_when_disabled(
    bot_app: MonitorBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock

    bot_app.settings = replace(
        bot_app.settings,
        webapp_enabled=False,
        webapp_public_origin="https://sentry.example.com",
    )
    bot_app._verify_after_reboot = AsyncMock()  # type: ignore[method-assign]
    bot_app._resume_apt_watches = AsyncMock()  # type: ignore[method-assign]
    setter = AsyncMock()
    monkeypatch.setattr(type(bot_app.app.bot), "set_chat_menu_button", setter)

    await bot_app._post_init(bot_app.app)
    setter.assert_awaited_once()
    assert setter.await_args is not None
    from telegram import MenuButtonDefault

    assert isinstance(setter.await_args.kwargs["menu_button"], MenuButtonDefault)


async def test_snapshot_cache_is_single_flight(
    bot_app: MonitorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Параллельные команды не должны запускать несколько сборов подряд."""
    import asyncio

    from stacksentry.models import MonitorSnapshot, PanelHealth

    calls = 0
    snapshot = MonitorSnapshot(
        system=None,
        services=[],
        panel=PanelHealth(
            reachable=False, status_code=None, latency_ms=None, error=None
        ),
        inbounds=[],
        subscriptions=[],
    )

    async def fake_snapshot() -> MonitorSnapshot:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return snapshot

    monkeypatch.setattr(bot_app.monitor, "snapshot", fake_snapshot)

    first, second = await asyncio.gather(bot_app.snapshot(), bot_app.snapshot())

    assert calls == 1
    assert first is second is snapshot
