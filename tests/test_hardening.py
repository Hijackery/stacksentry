"""Базовые защиты: то, что легко сломать правкой «на бегу».

Проверяются инварианты инфобеза, которые нельзя увидеть в юнит-тестах логики:
права файлов, изоляция сервиса, отсутствие широких sudo-правил, запрет
редиректов у клиента с токеном и привязка подтверждения к администратору.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from stacksentry import ui
from stacksentry.actions import ActionError, ActionManager
from stacksentry.collectors.panel import PanelClient, PanelError
from stacksentry.models import InboundSummary
from stacksentry.settings import BASE_DIR, Settings
from stacksentry.storage import Storage

UNIT = BASE_DIR / "install" / "stacksentry-bot.service"
SUDOERS = BASE_DIR / "install" / "stacksentry-sudoers"
WRAPPER = BASE_DIR / "install" / "stacksentry-ctl"


# ------------------------------------------------------------------ systemd


def test_unit_keeps_isolation_directives() -> None:
    """Юнит — основная граница песочницы сервиса, директивы не должны пропасть."""
    text = UNIT.read_text(encoding="utf-8")
    for directive in (
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/stacksentry",
        "ProtectHome=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectProc=invisible",
        "ProtectKernelTunables=true",
        "RestrictNamespaces=true",
        "RestrictAddressFamilies=",
        "SystemCallArchitectures=native",
        "UMask=0077",
        "StateDirectory=stacksentry",
        "User=stacksentry",
        "Group=stacksentry",
    ):
        assert directive in text, directive


def test_unit_does_not_hide_proc_stat() -> None:
    """ПроцSubset=pid прячет /proc/stat — psutil без него не соберёт метрики."""
    text = UNIT.read_text(encoding="utf-8")
    directives = [line for line in text.splitlines() if line.startswith("ProcSubset=")]
    assert directives == [], directives


def test_unit_does_not_use_root_and_keeps_sudo_usable() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "User=root" not in text
    assert "Group=root" not in text
    # NoNewPrivileges сломал бы sudo (setuid root) — привилегированные операции
    # бота перестали бы работать, поэтому запрет должен оставаться выключенным.
    assert "NoNewPrivileges=yes" not in text


# ------------------------------------------------------------------- sudo


def test_sudoers_is_narrow() -> None:
    text = SUDOERS.read_text(encoding="utf-8")
    # Команды только через обёртку, никаких ALL-правил.
    assert "NOPASSWD" in text
    assert "ALL=(ALL)" not in text
    command_lines = [
        line
        for line in text.splitlines()
        if line.strip().startswith("/") or line.strip().rstrip(",").endswith("*")
    ]
    assert command_lines, "в sudoers не нашлось ни одной команды"
    for line in command_lines:
        assert "stacksentry-ctl" in line, line
    # Внутренняя подкоманда transient-юнита не должна становиться доступной.
    assert "_apt_worker" not in text


# ----------------------------------------------------------------- клиент


async def test_panel_client_does_not_follow_redirects(
    make_settings: Callable[..., Settings],
) -> None:
    """Редирект увёл бы Bearer-токен панели на сторонний хост."""
    settings = make_settings()
    client = PanelClient(settings)
    session = client._session()
    try:
        assert session.follow_redirects is False
    finally:
        await client.close()


async def test_panel_client_reports_redirect_as_error(
    make_settings: Callable[..., Settings],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://evil.example/"})

    settings = make_settings()
    client = PanelClient(settings)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {settings.panel_api_token}"},
        follow_redirects=False,
    )
    with pytest.raises(PanelError, match="редирект"):
        await client.server_status()
    await client.close()


# ---------------------------------------------------------- подтверждения


def test_confirmation_is_bound_to_its_admin(
    make_settings: Callable[..., Settings],
) -> None:
    actions = ActionManager(make_settings())
    pending = actions.request("reboot", "перезагрузка", owner_id=111)

    with pytest.raises(ActionError, match="другой администратор"):
        actions.consume(pending.token, user_id=222)


def test_confirmation_still_works_for_owner(
    make_settings: Callable[..., Settings],
) -> None:
    actions = ActionManager(make_settings())
    pending = actions.request("reboot", "перезагрузка", owner_id=111)
    result = actions.consume(pending.token, user_id=111)
    assert result is not None
    assert result.owner_id == 111


# -------------------------------------------------------------- файлы


def test_state_database_is_readable_only_by_service_group(tmp_path: Path) -> None:
    """Webapp читает историю из базы бота, но писать в неё не должен."""
    path = tmp_path / "state.db"
    storage = Storage(path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o640, oct(mode)
    finally:
        storage.close()


def test_wal_files_are_private_too(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    storage = Storage(path)
    try:
        storage.audit("t", telegram_id=1, ok=True, result="x")
        for suffix in ("-wal", "-shm"):
            candidate = tmp_path / f"state.db{suffix}"
            if candidate.exists():
                assert stat.S_IMODE(os.stat(candidate).st_mode) == 0o600
    finally:
        storage.close()


# ------------------------------------------------------------- телеметрия


def test_logging_silences_token_leaking_libraries(
    make_settings: Callable[..., Settings],
) -> None:
    """telegram логирует URL с токеном — его логгер обязан быть приглушён."""
    from stacksentry.app import _setup_logging

    _setup_logging(make_settings(log_level="DEBUG"))
    for name in ("telegram", "telegram.ext", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING, name


# ------------------------------------------------------------------ HTML


def test_panel_supplied_text_is_escaped() -> None:
    """Текст из панели попадает в HTML Telegram — экранирование обязательно."""
    inbound = InboundSummary(
        remark="<b>зло</b>",
        protocol="vless",
        port=443,
        enabled=True,
        clients=1,
        up_bytes=1,
        down_bytes=1,
        total_bytes=0,
    )
    rendered = ui.render_inbound_detail(inbound)
    assert "<b>зло</b>" not in rendered
    assert "&lt;b&gt;зло&lt;/b&gt;" in rendered
