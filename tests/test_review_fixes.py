"""Регрессии по итогам ревью: подтверждения, таймауты, отмена, база, рендер.

Каждый тест здесь соответствует конкретной найденной ошибке — так при будущем
рефакторинге видно, что именно было сломано и зачем это защищено.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from telegram.error import RetryAfter

from stacksentry import ui
from stacksentry.actions import ActionError, ActionManager
from stacksentry.alerts import Finding, Rule
from stacksentry.app import _send_with_retry
from stacksentry.collectors.panel import PanelClient
from stacksentry.command import run_command
from stacksentry.models import CommandResult, Severity, SystemMetrics
from stacksentry.settings import ConfigError, Settings, load_settings
from stacksentry.storage import Storage, StorageError

# ------------------------------------------------------ подтверждения


def test_foreign_attempt_does_not_destroy_confirmation(
    make_settings: Callable[..., Settings],
) -> None:
    """Раньше чужое нажатие удаляло токен и ломало подтверждение админа."""
    actions = ActionManager(make_settings())
    pending = actions.request("reboot", "перезагрузка", owner_id=111)

    with pytest.raises(ActionError, match="другой администратор"):
        actions.consume(pending.token, user_id=222)

    assert actions.consume(pending.token, user_id=111) is not None


def test_cancel_reports_whether_it_had_anything_to_cancel(
    make_settings: Callable[..., Settings],
) -> None:
    actions = ActionManager(make_settings())
    pending = actions.request("reboot", "перезагрузка", owner_id=111)

    assert actions.cancel(pending.token) is True
    assert actions.cancel(pending.token) is False


async def test_restart_xray_respects_cooldown(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Раньше рестарт xray шёл мимо кулдауна и его можно было спамить."""

    class FakePanel:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def restart_xray(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr("stacksentry.actions.build_panel", FakePanel)
    actions = ActionManager(make_settings(action_cooldown=3600))

    await actions.restart_xray()
    with pytest.raises(ActionError, match="недавно"):
        await actions.restart_xray()


async def test_command_uses_configured_timeout(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMMAND_TIMEOUT из .env раньше игнорировался для произвольных команд."""
    seen: dict[str, float] = {}

    async def fake_run(
        argv: list[str], *, timeout: float, action: str
    ) -> CommandResult:
        seen["timeout"] = timeout
        return CommandResult(
            action=action,
            command=" ".join(argv),
            returncode=0,
            stdout="ok",
            stderr="",
            duration=0.0,
        )

    monkeypatch.setattr("stacksentry.actions.run_command", fake_run)
    actions = ActionManager(
        make_settings(command_allowlist=("free -h",), command_timeout=42)
    )

    await actions.run_allowed_command("free -h")
    assert seen["timeout"] == 42.0


# ------------------------------------------------ процессы: таймаут и отмена


class _FakeProcess:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.killed:
            return b"", b""
        if self.mode == "cancel":
            raise asyncio.CancelledError
        if self.mode == "asyncio-timeout":
            # На Python < 3.11 это отдельный класс, не встроенный TimeoutError.
            raise asyncio.TimeoutError
        await asyncio.sleep(30)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _patch_process(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> None:
    async def factory(*argv: object, **kwargs: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)


async def test_timeout_kills_process(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess("hang")
    _patch_process(monkeypatch, process)

    result = await run_command(["dummy"], timeout=0.05)

    assert result.returncode == 124
    assert process.killed is True


async def test_asyncio_timeout_error_is_also_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """На Python 3.10 asyncio бросал собственный TimeoutError.

    Ловить только встроенный было недостаточно: на 3.10 таймаут улетал наружу,
    команда не прерывалась, а дочерний процесс продолжал работать.
    """
    process = _FakeProcess("asyncio-timeout")
    _patch_process(monkeypatch, process)

    result = await run_command(["dummy"], timeout=30)

    assert result.returncode == 124
    assert process.killed is True


async def test_cancellation_kills_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остановка бота не должна оставлять работать дочерние процессы (apt и др.)."""
    process = _FakeProcess("cancel")
    _patch_process(monkeypatch, process)

    task = asyncio.create_task(run_command(["dummy"], timeout=30))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True


# --------------------------------------------------------------- flood control


async def test_flood_control_is_waited_out() -> None:
    """RetryAfter раньше просто терял сообщение; теперь ждём и повторяем."""
    calls = {"n": 0}

    async def sender(**kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(0)

    await _send_with_retry(sender, text="x")
    assert calls["n"] == 2


async def test_too_long_flood_control_does_not_hang() -> None:
    """Если Telegram просит ждать дольше MAX_FLOOD_WAIT — не подвисаем."""

    async def sender(**kwargs: object) -> None:
        raise RetryAfter(timedelta(seconds=120))

    with pytest.raises(RetryAfter):
        await _send_with_retry(sender, text="x")


# ------------------------------------------------------------ база данных


def test_busy_timeout_is_configured(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.db")
    try:
        value = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert value == 5000
    finally:
        storage.close()


def test_corrupted_database_reports_clearly(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    path.write_bytes(b"this is definitely not a sqlite database")

    with pytest.raises(StorageError, match="повреждена"):
        Storage(path)


# ------------------------------------------------------------------ рендер


def test_broken_metric_rows_do_not_crash_render() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT NULL AS bucket, NULL AS cpu, 'мусор' AS mem, 30 AS disk"
    ).fetchall()

    text = ui.render_history(rows, title="Срез")

    assert "—" in text
    assert "30%" in text
    conn.close()


def test_broken_audit_rows_do_not_crash_render() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT NULL AS ts, NULL AS telegram_id, 'reboot' AS action, 1 AS ok"
    ).fetchall()

    text = ui.render_audit(rows)

    assert "reboot" in text
    assert "—" in text
    conn.close()


# --------------------------------------------- пиннинг сертификата панели


def _capture_httpx_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Подменяет httpx.AsyncClient, чтобы увидеть аргументы подключения.

    Патчим именно модуль httpx: panel.py обращается к нему по имени, поэтому
    подмена видна и там (и снимается автоматически после теста).
    """
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return captured


def test_panel_uses_custom_ca_bundle(
    make_settings: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PANEL_CA_BUNDLE должен доходить до httpx как путь к CA-файлу."""
    bundle = tmp_path / "panel-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    captured = _capture_httpx_kwargs(monkeypatch)

    PanelClient(make_settings(panel_ca_bundle=str(bundle)))._session()

    assert captured["verify"] == str(bundle)


def test_panel_verifies_tls_by_default(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_httpx_kwargs(monkeypatch)

    PanelClient(make_settings())._session()

    assert captured["verify"] is True


def test_missing_ca_bundle_is_rejected(tmp_path: Path) -> None:
    """Опечатка в пути должна падать при старте, а не при первом запросе."""
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com\n"
        "PANEL_API_TOKEN=token\n"
        "PANEL_CA_BUNDLE=/nowhere/panel-ca.pem\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="PANEL_CA_BUNDLE"):
        load_settings(path)


def test_nonstandard_bot_api_is_warned(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    """Подменённый TELEGRAM_API_URL уводит токен бота — это должно быть видно."""
    from stacksentry.app import _warn_on_unusual_api

    settings = make_settings(telegram_api_url="http://127.0.0.1:8081")
    with caplog.at_level(logging.WARNING, logger="stacksentry.app"):
        _warn_on_unusual_api(settings)

    assert "неофициальный Bot API" in caplog.text


def test_official_bot_api_is_not_warned(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    from stacksentry.app import _warn_on_unusual_api

    with caplog.at_level(logging.WARNING, logger="stacksentry.app"):
        _warn_on_unusual_api(make_settings())

    assert "неофициальный" not in caplog.text


# ------------------------------------------------- обновление с phased


async def test_phased_upgrade_goes_through_privileged_wrapper(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка phased-обновления обязана идти через ту же обёртку и allowlist."""
    seen: list[list[str]] = []

    async def fake_run(
        argv: list[str], *, timeout: float, action: str
    ) -> CommandResult:
        seen.append(list(argv))
        return CommandResult(
            action=action,
            command=" ".join(argv),
            returncode=0,
            stdout="",
            stderr="",
            duration=0.0,
        )

    monkeypatch.setattr("stacksentry.actions.run_command", fake_run)
    actions = ActionManager(make_settings())

    await actions.apt_upgrade_phased()

    assert seen[0][0] == "sudo"
    assert seen[0][3] == "/usr/local/bin/stacksentry-ctl"
    assert seen[0][4] == "apt_upgrade_phased"


# ------------------------------------------------------------ UX-инварианты


def test_reboot_is_reachable_from_menu() -> None:
    """Критично: reboot обрабатывался в коде, но кнопки в интерфейсе не было."""
    data = [
        button.callback_data
        for row in ui.maintenance_menu().inline_keyboard
        for button in row
    ]
    assert "ask:reboot:" in data


def test_services_menu_has_journal_next_to_restart() -> None:
    data = [
        button.callback_data
        for row in ui.services_menu(("x-ui",), is_admin=True).inline_keyboard
        for button in row
    ]
    assert "ask:restart_service:x-ui" in data
    assert "logs:x-ui" in data


def test_confirm_menu_puts_cancel_first() -> None:
    """Опасная кнопка не должна стоять первой там, куда попадает палец."""
    first = ui.confirm_menu("reboot", "tok").inline_keyboard[0][0]
    assert first.callback_data == "no:tok"


def test_alert_has_actions() -> None:
    data = [
        button.callback_data
        for row in ui.alert_menu().inline_keyboard
        for button in row
    ]
    assert "refresh" in data
    assert "home" in data


def test_system_block_is_monospaced(
    make_settings: Callable[..., Settings],
) -> None:
    """Метрики в <code>: колонки не «едут» при разной длине значений."""
    metrics = SystemMetrics(
        cpu_percent=12.0,
        mem_percent=41.0,
        mem_total_bytes=8 * 1024**3,
        swap_percent=0.0,
        disk_percent=63.0,
        disk_free_bytes=30 * 1024**3,
        load1=0.5,
        uptime_seconds=3600,
        tcp_connections=10,
        net_sent_bytes=1024,
        net_recv_bytes=2048,
    )

    text = ui.render_system(metrics, make_settings())

    assert text.startswith("<code>") and text.endswith("</code>")
    assert "Диск" in text


# ------------------------------------------- действия и мьюты в алертах


def test_mute_suppresses_and_expires(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.db")
    try:
        storage.mute("service:x-ui", 3600)
        assert storage.is_muted("service:x-ui") is True
        storage.mute("service:x-ui", -1)  # уже истёк
        assert storage.is_muted("service:x-ui") is False
    finally:
        storage.close()


def test_alert_actions_offer_restart_for_service_finding(
    make_settings: Callable[..., Settings],
) -> None:
    """Сигнал без действия заставляет ночью искать раздел руками."""
    from stacksentry.app import _alert_actions

    finding = Finding(
        rule=Rule.service,
        severity=Severity.critical,
        title="Сервис x-ui не работает",
        detail="ActiveState=failed",
        key="service:x-ui",
    )
    buttons = _alert_actions([finding], make_settings(services=("x-ui",)))
    data = [button.callback_data for button in buttons]

    assert "ask:restart_service:x-ui" in data
    assert "logs:x-ui" in data


def test_alert_menu_has_feedback_and_snooze() -> None:
    from telegram import InlineKeyboardButton

    markup = ui.alert_menu(
        [
            InlineKeyboardButton(
                "♻️ Рестарт x-ui", callback_data="ask:restart_service:x-ui"
            )
        ],
        alert_key="service:x-ui",
    )
    data = [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "ask:restart_service:x-ui" in data
    assert "falsepos:service:x-ui" in data
    assert "snooze:10:service:x-ui" in data
    assert "snooze:180:service:x-ui" in data
    assert "mute:service:x-ui" not in data
    assert "refresh" in data


def test_audit_can_be_filtered(tmp_path: Path) -> None:
    """Разбор инцидента начинается с фильтра, а не с чтения всего подряд."""
    storage = Storage(tmp_path / "state.db")
    try:
        storage.audit("denied", telegram_id=999, ok=False, result="гость")
        storage.audit("reboot", telegram_id=42, ok=True, result="ok")

        denied = storage.recent_audit(10, only="denied")
        actions = storage.recent_audit(10, only="actions")

        assert [row["action"] for row in denied] == ["denied"]
        assert [row["action"] for row in actions] == ["reboot"]
    finally:
        storage.close()


def test_overview_menu_toggles_details() -> None:
    compact = [
        b.callback_data
        for row in ui.overview_menu(True, compact=True).inline_keyboard
        for b in row
    ]
    full = [
        b.callback_data
        for row in ui.overview_menu(True, compact=False).inline_keyboard
        for b in row
    ]

    assert "details" in compact
    assert "refresh" in full


def test_log_view_menu_offers_filters() -> None:
    data = [
        b.callback_data for row in ui.log_view_menu("x-ui").inline_keyboard for b in row
    ]
    assert "logs_err:x-ui" in data
    assert "logs200:x-ui" in data


# ------------------------------------------- адрес панели: слеши и base path


def test_base_path_cannot_be_an_address(tmp_path: Path) -> None:
    """Регрессия: адрес в webBasePath приводил к непонятному HTTP-сбою."""
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com:2053\n"
        "PANEL_BASE_PATH=https://panel.example.com:2053/\n"
        "PANEL_API_TOKEN=token\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="PANEL_BASE_PATH"):
        load_settings(path)


def test_extra_slashes_do_not_build_double_slash_urls(tmp_path: Path) -> None:
    """Лишние слеши в .env не должны превращаться в «//» в адресе API."""
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com:2053///\n"
        "PANEL_BASE_PATH=/panel/\n"
        "PANEL_API_TOKEN=token\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.api_base == "https://panel.example.com:2053/panel/panel/api"
    assert "//" not in settings.api_base.split("://", 1)[1]


def test_dirty_panel_values_are_normalized_on_read(tmp_path: Path) -> None:
    """Даже вручную вписанные слеши не должны давать «//» в адресе API."""
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com:5580///\n"
        "PANEL_BASE_PATH=/panel/\n"
        "PANEL_API_TOKEN=token\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.panel_url == "https://panel.example.com:5580"
    assert settings.panel_base_path == "panel"
    assert settings.api_base == "https://panel.example.com:5580/panel/panel/api"


# ------------------------------------- журнал сборщиков не забивается повторами


def test_repeated_collector_errors_are_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Пропавший /proc/stat раньше писал полный трейсбек каждую минуту."""
    from stacksentry import monitor as monitor_module

    monitor_module._LOGGED_ERRORS.clear()
    error = FileNotFoundError("[Errno 2] No such file or directory: '/proc/stat'")

    with caplog.at_level(logging.DEBUG, logger="stacksentry.monitor"):
        for _ in range(3):
            monitor_module._log_collector_error(
                "metrics", "Не удалось снять системные метрики", error
            )

    errors = [item for item in caplog.records if item.levelno == logging.ERROR]
    assert len(errors) == 1, [item.getMessage() for item in caplog.records]


def test_proc_stat_error_gets_a_hint() -> None:
    """Пользователь должен сразу понять, что дело в юните службы."""
    from stacksentry.monitor import _proc_hint

    hint = _proc_hint(
        FileNotFoundError("[Errno 2] No such file or directory: '/proc/stat'")
    )
    assert "ProcSubset" in hint
    assert _proc_hint(ValueError("другая ошибка")) == ""


# ------------------------------------------ подсказки по версии панели


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("3.10.2", "Учётная запись"),
        ("v3.0.0", "Учётная запись"),
        ("2.8.5", "API Tokens"),
        ("", "API Tokens"),
        ("неизвестно", "API Tokens"),
    ],
)
def test_token_hint_matches_panel_version(version: str, expected: str) -> None:
    """Интерфейс v3 и 2.x отличается: подсказка должна соответствовать версии."""
    from stacksentry.hints import UNKNOWN_PANEL_HINT, panel_token_hint

    hint = panel_token_hint(version)
    if version in ("", "неизвестно"):
        assert hint == UNKNOWN_PANEL_HINT
        assert "API Tokens" in hint
    else:
        assert expected in hint, hint


async def test_timeout_kills_grandchildren_holding_pipes(tmp_path: Path) -> None:
    """Внук (например do-release-upgrade → apt) не переживает таймаут.

    Раньше kill бил только прямой sudo: внук держал пайпы открытыми, дочитать
    вывод не удавалось, и фоновая задача падала с UnboundLocalError — в чате это
    выглядело как «зависло». Теперь гасится вся группа процессов, а stdout/stderr
    заведены заранее, поэтому результат возвращается даже при нечитаемом выводе.
    """
    pidfile = tmp_path / "child.pid"
    result = await run_command(
        ["bash", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=0.5
    )

    assert result.returncode == 124
    await asyncio.sleep(0.3)
    child = int(pidfile.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
