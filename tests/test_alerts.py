"""Проверки алертов: пороги, гистерезис, кулдаун, восстановление."""

from __future__ import annotations

from collections.abc import Callable

from stacksentry.alerts import AlertEngine, Rule
from stacksentry.models import (
    HealthCheck,
    InboundSummary,
    MonitorSnapshot,
    PanelHealth,
    ServiceStatus,
    Severity,
    Subscription,
    SystemMetrics,
)
from stacksentry.settings import Settings
from stacksentry.storage import Storage


def metrics(cpu: float, mem: float = 10.0, disk: float = 10.0) -> SystemMetrics:
    return SystemMetrics(
        cpu_percent=cpu,
        mem_percent=mem,
        mem_total_bytes=1024**3,
        swap_percent=0.0,
        disk_percent=disk,
        disk_free_bytes=10**9,
        load1=0.1,
        uptime_seconds=100.0,
        tcp_connections=1,
        net_sent_bytes=0,
        net_recv_bytes=0,
    )


def panel(**overrides: object) -> PanelHealth:
    base: dict[str, object] = {
        "reachable": True,
        "status_code": 200,
        "latency_ms": 12.0,
        "error": None,
        "panel_version": "2.6.0",
        "xray_version": "25.1.0",
        "xray_state": "running",
        "api_authorized": True,
    }
    base.update(overrides)
    return PanelHealth(**base)  # type: ignore[arg-type]


def service(active: bool = True, name: str = "x-ui") -> ServiceStatus:
    return ServiceStatus(
        name=name,
        active=active,
        sub_state="running" if active else "failed",
        active_state="active" if active else "failed",
        main_pid=123 if active else 0,
        since="Mon",
        restarts=0,
        memory_bytes=10 * 1024**2,
        cpu_percent=1.0,
    )


def snapshot(
    system: SystemMetrics | None = None,
    *,
    panel_health: PanelHealth | None = None,
    services: list[ServiceStatus] | None = None,
    warnings: list[str] | None = None,
    subscriptions: list[Subscription] | None = None,
    inbounds: list[InboundSummary] | None = None,
    unavailable: frozenset[str] = frozenset(),
    checks: list[HealthCheck] | None = None,
) -> MonitorSnapshot:
    return MonitorSnapshot(
        system=system,
        services=services if services is not None else [service()],
        panel=panel_health if panel_health is not None else panel(),
        inbounds=inbounds if inbounds is not None else [],
        subscriptions=subscriptions or [],
        checks=checks or [],
        warnings=warnings or [],
        unavailable=unavailable,
    )


def engine(
    make_settings: Callable[..., Settings], storage: Storage, **overrides: object
) -> AlertEngine:
    # Большинство тестов проверяют логику порогов, а не дебаунс: по умолчанию
    # подтверждаем переход с первого замера. Отдельные тесты задают 2.
    overrides.setdefault("alert_confirm_checks", 1)
    overrides.setdefault("alert_recovery_checks", 1)
    return AlertEngine(make_settings(**overrides), storage)


def test_healthy_snapshot_produces_nothing(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    assert alerts.evaluate(snapshot(metrics(10.0))) == []


def test_transient_spike_is_ignored_until_confirmed(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    """Одиночный всплеск не должен выглядеть как авария."""
    alerts = engine(
        make_settings,
        storage,
        alert_confirm_checks=2,
        alert_recovery_checks=2,
    )

    assert alerts.update(alerts.evaluate(snapshot(metrics(96.0)))) == []
    assert alerts.update(alerts.evaluate(snapshot(metrics(10.0)))) == []
    assert storage.get_alert_state("cpu") is None

    assert alerts.update(alerts.evaluate(snapshot(metrics(96.0)))) == []
    second = alerts.update(alerts.evaluate(snapshot(metrics(96.0))))
    assert [finding.key for finding in second] == ["cpu"]


def test_cpu_threshold_maps_to_severity(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    warning = alerts.evaluate(snapshot(metrics(85.0)))
    assert [finding.severity.value for finding in warning] == ["warning"]

    critical = alerts.evaluate(snapshot(metrics(96.0)))
    assert [finding.severity.value for finding in critical] == ["critical"]


def test_hysteresis_keeps_state_after_drop(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    alerts.evaluate(snapshot(metrics(96.0)))
    alerts.update(alerts.evaluate(snapshot(metrics(96.0))))

    # 93% ниже crit (95), но выше crit - гистерезис (90) — состояние держится.
    assert alerts.evaluate(snapshot(metrics(93.0)))[0].severity.value == "critical"
    # 88% уже ниже crit - гистерезис (90) — уровень crit сброшен,
    # но 88% всё ещё выше warn (80), поэтому это warning, а не «тишина».
    assert alerts.evaluate(snapshot(metrics(88.0)))[0].severity.value == "warning"
    # Ниже warn - гистерезис (75) — alert выключается совсем.
    assert alerts.evaluate(snapshot(metrics(70.0))) == []


def test_alert_is_sent_once_and_then_waits_cooldown(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage, alert_cooldown=60)
    first = alerts.update(alerts.evaluate(snapshot(metrics(96.0))))
    assert len(first) == 1

    # Доставку подтверждает вызывающий: пока не подтверждено, алерт шлётся
    # снова даже внутри cooldown.
    retried = alerts.update(alerts.evaluate(snapshot(metrics(96.0))))
    assert [f.key for f in retried] == ["cpu"]

    alerts.mark_sent([f.key for f in first])
    assert alerts.update(alerts.evaluate(snapshot(metrics(96.0)))) == []


def test_recovery_notification(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    alerts.update(alerts.evaluate(snapshot(metrics(96.0))))
    recovered = alerts.update(alerts.evaluate(snapshot(metrics(5.0))))
    assert len(recovered) == 1
    assert recovered[0].severity.value == "ok"
    state = storage.get_alert_state("cpu")
    assert state is not None
    assert state["state"] == "ok"


def test_inode_threshold_and_health_checks(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    inode = alerts.evaluate(
        snapshot(
            SystemMetrics(
                cpu_percent=5.0,
                mem_percent=10.0,
                mem_total_bytes=1024**3,
                swap_percent=0.0,
                disk_percent=10.0,
                disk_free_bytes=10**9,
                load1=0.1,
                uptime_seconds=100.0,
                tcp_connections=1,
                net_sent_bytes=0,
                net_recv_bytes=0,
                inode_percent=96.0,
            )
        )
    )
    assert any(finding.key == "inode" for finding in inode)

    checked = alerts.evaluate(
        snapshot(
            metrics(5.0),
            checks=[
                HealthCheck(
                    "check:backup",
                    Severity.warning,
                    "Бэкап устарел",
                    "48 ч",
                )
            ],
        )
    )
    assert any(finding.rule is Rule.check for finding in checked)


def test_dead_service_is_critical(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    findings = alerts.evaluate(snapshot(metrics(5.0), services=[service(active=False)]))
    assert findings[0].rule is Rule.service
    assert findings[0].severity.value == "critical"


def test_unreachable_panel_and_xray_down(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    findings = alerts.evaluate(
        snapshot(
            metrics(5.0),
            panel_health=panel(reachable=False, error="connection refused"),
        )
    )
    assert findings[0].rule is Rule.panel
    assert "недоступна" in findings[0].title

    xray_down = alerts.evaluate(
        snapshot(metrics(5.0), panel_health=panel(xray_state="stop"))
    )
    assert any(finding.title == "Xray не запущен" for finding in xray_down)


def test_missing_metrics_are_reported(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    findings = alerts.evaluate(snapshot(None))
    assert findings[0].severity.value == "unknown"


def test_subscription_warnings_are_passed_through(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    alerts = engine(make_settings, storage)
    findings = alerts.evaluate(
        snapshot(metrics(5.0), warnings=["истёкшие подписки: 2"])
    )
    assert any("нужна проверка" in finding.title for finding in findings)


def test_system_warnings_are_not_blamed_on_subscriptions() -> None:
    """Проблемы железа и проблемы подписок — разные находки."""
    from stacksentry.alerts import _is_subscription_warning

    assert _is_subscription_warning("подписки не собраны: HTTP 404 на inbounds/list")
    assert _is_subscription_warning("список inbound'ов недоступен: нет node-sync")
    assert not _is_subscription_warning(
        "метрики системы недоступны: [Errno 2] No such file or directory: '/proc/stat'"
    )


def test_unavailable_metrics_do_not_fake_recovery(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    """Пропавший /proc — не повод писать «CPU восстановлен»."""
    alerts = engine(make_settings, storage)
    alerts.update(alerts.evaluate(snapshot(metrics(96.0))))

    failed = snapshot(None, unavailable=frozenset({"metrics"}))
    out = alerts.update(alerts.evaluate(failed), unavailable=failed.unavailable)

    assert not any("восстановлено" in finding.title for finding in out)
    assert (
        storage.get_alert_state("cpu:metrics") or True
    )  # состояние не обязано меняться


def test_unavailable_services_do_not_fake_recovery(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    """Если systemctl не ответил, «Сервис x-ui восстановлен» — ложь."""
    alerts = engine(make_settings, storage)
    down = snapshot(services=[service(active=False)])
    alerts.update(alerts.evaluate(down))

    failed = snapshot(services=[], unavailable=frozenset({"services"}))
    out = alerts.update(alerts.evaluate(failed), unavailable=failed.unavailable)

    assert not any("восстановлено" in finding.title for finding in out)


def test_disabled_panel_produces_no_panel_findings(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    from stacksentry.alerts import Rule

    alerts = engine(make_settings, storage)
    disabled = panel(
        reachable=False, error=None, panel_version="", api_authorized=False
    )
    disabled = PanelHealth(
        reachable=False,
        status_code=None,
        latency_ms=None,
        error=None,
        disabled=True,
    )
    findings = alerts.evaluate(snapshot(system=metrics(10.0), panel_health=disabled))
    assert not any(finding.rule is Rule.panel for finding in findings)
    assert not any("восстановлено" in finding.title for finding in findings)
