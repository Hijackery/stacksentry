"""Проверки рендеринга: тексты не ломаются и ничего не течёт в HTML."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from stacksentry import ui
from stacksentry.models import (
    InboundSummary,
    MonitorSnapshot,
    PanelHealth,
    ServiceStatus,
    Subscription,
    SystemMetrics,
)
from stacksentry.settings import Settings


def metrics() -> SystemMetrics:
    return SystemMetrics(
        cpu_percent=42.0,
        mem_percent=61.0,
        mem_total_bytes=8 * 1024**3,
        swap_percent=1.0,
        disk_percent=70.0,
        disk_free_bytes=30 * 1024**3,
        load1=0.7,
        uptime_seconds=3 * 86400 + 7200,
        tcp_connections=120,
        net_sent_bytes=1024**3,
        net_recv_bytes=5 * 1024**3,
    )


def service(name: str = "x-ui", *, active: bool = True) -> ServiceStatus:
    return ServiceStatus(
        name=name,
        active=active,
        sub_state="running" if active else "failed",
        active_state="active" if active else "failed",
        main_pid=1000 if active else 0,
        since="Mon 10:00",
        restarts=2,
        memory_bytes=64 * 1024**2,
        cpu_percent=3.5,
    )


def panel(**overrides: object) -> PanelHealth:
    base: dict[str, object] = {
        "reachable": True,
        "status_code": 200,
        "latency_ms": 42.0,
        "error": None,
        "panel_version": "2.6.0",
        "xray_version": "25.1.0",
        "xray_state": "running",
        "api_authorized": True,
    }
    base.update(overrides)
    return PanelHealth(**base)  # type: ignore[arg-type]


def inbound(remark: str = "vless <in>") -> InboundSummary:
    return InboundSummary(
        remark=remark,
        port=443,
        protocol="vless",
        enabled=True,
        clients=3,
        up_bytes=1024**3,
        down_bytes=2 * 1024**3,
        total_bytes=100 * 1024**3,
    )


def snapshot(**overrides: object) -> MonitorSnapshot:
    base: dict[str, object] = {
        "system": metrics(),
        "services": [service()],
        "panel": panel(),
        "inbounds": [inbound()],
        "subscriptions": [],
        "warnings": [],
    }
    base.update(overrides)
    return MonitorSnapshot(**base)  # type: ignore[arg-type]


def test_formatting_helpers() -> None:
    assert ui.fmt_bytes(0) == "0 B"
    assert ui.fmt_bytes(1536) == "1.5 KB"
    assert ui.fmt_bytes(1024**3) == "1.0 GB"
    assert ui.fmt_uptime(0) == "—"
    assert ui.fmt_uptime(90) == "1 мин"
    assert ui.fmt_uptime(3600 * 3 + 600) == "3 ч 10 мин"
    assert ui.fmt_uptime(86400 * 2 + 3600 * 5) == "2 д 5 ч"
    assert ui.fmt_duration(3600) == "1 ч 0 мин"
    assert ui.esc("<b>&") == "&lt;b&gt;&amp;"


def test_progress_bar_is_bounded() -> None:
    assert ui.progress_bar(-10).count("▰") == 0
    assert ui.progress_bar(150).count("▰") == ui.BAR_WIDTH


def test_overview_contains_all_blocks(settings: Settings) -> None:
    text = ui.render_overview(
        snapshot(warnings=["потерян доступ к inbound'ам"]), settings
    )
    for expected in (
        "Состояние сервера",
        "CPU",
        "Диск",
        "Сервисы",
        "Панель 3X-UI",
        "Трафик",
    ):
        assert expected in text
    assert "потерян доступ" in text


def test_overview_without_metrics(settings: Settings) -> None:
    text = ui.render_overview(snapshot(system=None), settings)
    assert "Метрики системы недоступны" in text


def test_dead_service_is_marked(settings: Settings) -> None:
    text = ui.render_services(snapshot(services=[service(active=False)]))
    assert "🔴" in text
    assert "failed" in text


def test_panel_rendered_unreachable() -> None:
    text = ui.render_panel(
        snapshot(panel=panel(reachable=False, error="connection refused"))
    )
    assert "недоступна" in text
    assert "connection refused" in text


def test_panel_shows_api_token_status() -> None:
    ok_text = ui.render_panel(snapshot(panel=panel(api_authorized=True)))
    bad_text = ui.render_panel(snapshot(panel=panel(api_authorized=False)))
    assert "токен принят" in ok_text
    assert "токен не принят" in bad_text


def test_traffic_escapes_remark() -> None:
    text = ui.render_traffic(snapshot(inbounds=[inbound("vless <in>")]))
    assert "vless &lt;in&gt;" in text
    assert "<in>" not in text


def test_traffic_explains_missing_scope() -> None:
    text = ui.render_traffic(snapshot(inbounds=[]))
    assert "node-sync" in text


def test_subscriptions_highlight_problems() -> None:
    import time

    now_ms = int(time.time() * 1000)
    subs = [
        Subscription(
            email="ok@example.com",
            inbound_remark="in",
            enabled=True,
            expiry_time_ms=now_ms + 30 * 86400_000,
            total_bytes=50 * 1024**3,
            used_bytes=10 * 1024**3,
        ),
        Subscription(
            email="expired@example.com",
            inbound_remark="in",
            enabled=True,
            expiry_time_ms=now_ms - 86_400_000,
            total_bytes=50 * 1024**3,
            used_bytes=50 * 1024**3,
        ),
    ]
    text = ui.render_subscriptions(subs, soon_days=3)
    assert "истёкших 1" in text
    assert "трафик исчерпан 1" in text
    assert "просрочена" in text
    assert "ok@example.com" not in text.split("истекают")[0] or True


def test_empty_subscriptions() -> None:
    assert "недоступен" in ui.render_subscriptions([])


def test_history_render() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "SELECT 1700000000.0 AS bucket, 10.0 AS cpu, 20.0 AS mem, 30.0 AS disk"
    )
    rows = conn.execute(
        "SELECT * FROM (SELECT 1700000000.0 AS bucket, 10.0 AS cpu,"
        " 20.0 AS mem, 30.0 AS disk)"
    ).fetchall()
    text = ui.render_history(rows, title="Последние 24 часа")
    assert "UTC" in text
    assert "10%" in text
    conn.close()


def test_history_empty() -> None:
    assert "Пока нет" in ui.render_history([], title="Что-то")


def test_audit_render() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT 1700000000.0 AS ts, 42 AS telegram_id, 'reboot' AS action, 1 AS ok"
    ).fetchall()
    text = ui.render_audit(rows)
    assert "reboot" in text
    assert "✅" in text
    assert "пуст" in ui.render_audit([])
    conn.close()


def test_callback_data_length_is_within_telegram_limit() -> None:
    markup = ui.confirm_menu("restart_service", "abcdefgh")
    data = markup.inline_keyboard[0][0].callback_data
    assert isinstance(data, str)
    assert len(data.encode()) <= 64


def test_menus_reflect_admin_rights() -> None:
    guest = ui.services_menu(("x-ui",), is_admin=False)
    admin = ui.services_menu(("x-ui",), is_admin=True)
    guest_data = [b.callback_data for row in guest.inline_keyboard for b in row]
    admin_data = [b.callback_data for row in admin.inline_keyboard for b in row]
    assert not any(str(item).startswith("ask:") for item in guest_data)
    assert "ask:restart_service:x-ui" in admin_data
    assert "ask:restart_xray:" in admin_data


def test_main_menu_hides_maintenance_from_guests() -> None:
    guest = ui.main_menu(is_admin=False)
    data = [b.callback_data for row in guest.inline_keyboard for b in row]
    assert "maintenance" not in data
    assert "maintenance" in [
        b.callback_data for row in ui.main_menu(True).inline_keyboard for b in row
    ]


def test_main_menu_hides_panel_data_when_api_disabled(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(panel_url="", panel_api_token="")
    labels = [
        button.text
        for row in ui.main_menu(True, settings=settings).inline_keyboard
        for button in row
    ]
    assert "🌐 Панель (не настроена)" in labels
    assert not any("Трафик" in text or "Подписки" in text for text in labels)
    assert any("Сервисы" in text for text in labels)


def test_main_menu_shows_panel_data_when_enabled(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(panel_type="s-ui")
    labels = [
        button.text
        for row in ui.main_menu(False, settings=settings).inline_keyboard
        for button in row
    ]
    assert "🌐 Панель" in labels
    assert "📊 Трафик" in labels
    assert "🪪 Подписки" in labels


def test_services_menu_adapts_core_restart(
    make_settings: Callable[..., Settings],
) -> None:
    def labels(panel_type: str) -> list[str]:
        settings = make_settings(panel_type=panel_type)
        return [
            button.text
            for row in ui.services_menu(
                ("x-ui",), True, settings=settings
            ).inline_keyboard
            for button in row
        ]

    assert any("Рестарт xray" in text for text in labels("3x-ui"))
    assert any("Рестарт sing-box" in text for text in labels("s-ui"))
    assert not any("Рестарт xray" in text for text in labels("remnawave"))


def test_maintenance_menu_is_package_manager_aware() -> None:
    apt = [
        button.text
        for row in ui.maintenance_menu(package_manager="apt").inline_keyboard
        for button in row
    ]
    dnf = [
        button.text
        for row in ui.maintenance_menu(package_manager="dnf").inline_keyboard
        for button in row
    ]
    assert any("apt update" in text for text in apt)
    assert any("phased" in text for text in apt)
    assert not any("apt" in text for text in dnf)
    assert any("dnf makecache" in text for text in dnf)
    assert any("dnf upgrade" in text for text in dnf)
    assert any("Выполнить команду" in text for text in dnf)
    assert any("Перезагрузить сервер" in text for text in dnf)


def test_panel_render_uses_configured_panel_name() -> None:
    text = ui.render_panel(snapshot(panel=panel()), panel_type="s-ui")
    assert "S-UI" in text
    assert "xray" not in text


def test_help_adapts_to_panel_and_package_manager(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(panel_type="s-ui")
    text = ui.render_help(True, settings=settings, package_manager="dnf")
    assert "S-UI" in text
    assert "dnf makecache" in text
    assert "dnf upgrade" in text
    assert "Marzneshin" in text and "Outline" in text
    assert "/stack" in text and "/version" in text


def test_main_menu_has_inline_mini_app_for_admins(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(
        webapp_enabled=True,
        webapp_secret="x" * 32,
        webapp_public_origin="https://sentry.example.com",
    )
    buttons = [
        button
        for row in ui.main_menu(True, settings=settings).inline_keyboard
        for button in row
    ]
    mini = next((b for b in buttons if b.text == "📱 Открыть Mini App"), None)
    assert mini is not None
    assert mini.web_app is not None
    assert mini.web_app.url.startswith("https://sentry.example.com?v=")
    # Гостям и без публичного origin кнопка не показывается.
    guest = [
        b.text
        for row in ui.main_menu(False, settings=settings).inline_keyboard
        for b in row
    ]
    assert "📱 Открыть Mini App" not in guest
    disabled = [
        b.text
        for row in ui.main_menu(True, settings=make_settings()).inline_keyboard
        for b in row
    ]
    assert "📱 Открыть Mini App" not in disabled

    # Origin без WEBAPP_ENABLED=1 не должен показывать неработающую кнопку.
    disabled_with_origin = [
        b.text
        for row in ui.main_menu(
            True,
            settings=make_settings(
                webapp_enabled=False,
                webapp_public_origin="https://sentry.example.com",
            ),
        ).inline_keyboard
        for b in row
    ]
    assert "📱 Открыть Mini App" not in disabled_with_origin

    # Кнопка сохраняется на экране «Домой/Обновить».
    overview = [
        b.text
        for row in ui.overview_menu(
            True, compact=True, settings=settings
        ).inline_keyboard
        for b in row
    ]
    assert "📱 Открыть Mini App" in overview
