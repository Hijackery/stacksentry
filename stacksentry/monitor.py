"""Сборка полного среза состояния сервера и панели."""

from __future__ import annotations

import asyncio
import logging

from .collectors.health import HealthCollector
from .collectors.panels import build_panel
from .collectors.services import ServiceCollector
from .collectors.subscriptions import SubscriptionCollector
from .collectors.system import SystemCollector
from .models import (
    HealthCheck,
    InboundSummary,
    MonitorSnapshot,
    PanelHealth,
    ServiceStatus,
    Subscription,
    SystemMetrics,
)
from .settings import Settings

logger = logging.getLogger(__name__)


#: Последняя ошибка каждого сборщика: повторные сбои не льём в журнал целиком.
_LOGGED_ERRORS: dict[str, str] = {}


def _log_collector_error(key: str, message: str, exc: Exception) -> None:
    """Полный трейсбек — только при новой ошибке, повторы — на уровне DEBUG.

    Иначе сбойный сбор (например, пропавший /proc/stat) пишет простыню в журнал
    каждую минуту, и в ней тонут остальные записи.
    """
    text = f"{type(exc).__name__}: {exc}"
    if _LOGGED_ERRORS.get(key) == text:
        logger.debug("%s (повтор): %s", message, text)
        return
    _LOGGED_ERRORS[key] = text
    logger.error(message, exc_info=exc)


def _clear_collector_error(key: str) -> None:
    _LOGGED_ERRORS.pop(key, None)


def _proc_hint(exc: Exception) -> str:
    """Подсказка для известной причины: юнит со старой директивой ProcSubset."""
    if "/proc/stat" in str(exc):
        return (
            " — проверьте юнит службы: старая директива ProcSubset прячет /proc/stat; "
            "обновите установку или удалите эту строку и перезапустите службу"
        )
    return ""


class Monitor:
    """Опрашивает все источники и возвращает один снимок.

    Каждый источник изолирован: недоступность панели не должна превращаться
    в пустой отчёт по серверу, и наоборот.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.system = SystemCollector(settings.disk_path)
        self.services = ServiceCollector(settings.services)
        self.panel = build_panel(settings)
        self.subscriptions = SubscriptionCollector(self.panel)
        self.health = HealthCollector(settings)

    async def close(self) -> None:
        await self.panel.close()

    async def snapshot(self) -> MonitorSnapshot:
        metrics_warnings: list[str] = []
        services_warnings: list[str] = []
        panel_warnings: list[str] = []
        metrics_unavailable: set[str] = set()
        services_unavailable: set[str] = set()
        panel_unavailable: set[str] = set()
        checks_warnings: list[str] = []
        checks_unavailable: set[str] = set()

        metrics, services, panel_bundle, checks = await asyncio.gather(
            self._collect_metrics(metrics_warnings, metrics_unavailable),
            self._collect_services(services_warnings, services_unavailable),
            self._collect_panel_bundle(panel_warnings, panel_unavailable),
            self._collect_checks(checks_warnings, checks_unavailable),
        )
        health, inbounds, subs = panel_bundle

        warnings = [
            *metrics_warnings,
            *services_warnings,
            *panel_warnings,
            *checks_warnings,
        ]
        if subs or inbounds:
            warnings.extend(
                SubscriptionCollector.problems(
                    inbounds,
                    subs,
                    expiring_soon_days=self.settings.expiring_soon_days,
                    inbound_api=self.settings.panel_type == "3x-ui",
                )
            )

        return MonitorSnapshot(
            system=metrics,
            services=services,
            panel=health,
            inbounds=inbounds,
            subscriptions=subs,
            checks=checks,
            warnings=warnings,
            unavailable=frozenset(
                metrics_unavailable
                | services_unavailable
                | panel_unavailable
                | checks_unavailable
            ),
        )

    async def _collect_checks(
        self, warnings: list[str], unavailable: set[str]
    ) -> list[HealthCheck]:
        try:
            return await self.health.collect()
        except Exception as exc:
            _log_collector_error("checks", "Дополнительные проверки упали", exc)
            warnings.append(f"дополнительные проверки недоступны: {exc}")
            unavailable.add("checks")
            return []

    async def _collect_panel_bundle(
        self, warnings: list[str], unavailable: set[str]
    ) -> tuple[PanelHealth, list[InboundSummary], list[Subscription]]:
        """Проверка панели и подписки идут параллельно.

        У них общий HTTP-клиент, поэтому после подготовки сессии запросы можно
        запускать одновременно: медленный health больше не задерживает сбор
        подписок и наоборот.
        """
        health_warnings: list[str] = []
        subs_warnings: list[str] = []
        health_unavailable: set[str] = set()
        subs_unavailable: set[str] = set()
        health, (inbounds, subs) = await asyncio.gather(
            self._collect_panel_health(health_warnings, health_unavailable),
            self._collect_subscriptions(subs_warnings, subs_unavailable),
        )
        warnings.extend(health_warnings)
        warnings.extend(subs_warnings)
        unavailable.update(health_unavailable)
        unavailable.update(subs_unavailable)
        return health, inbounds, subs

    async def _collect_metrics(
        self, warnings: list[str], unavailable: set[str]
    ) -> SystemMetrics | None:
        try:
            result = await asyncio.to_thread(self.system.collect)
            _clear_collector_error("metrics")
            return result
        except Exception as exc:  # сбор не должен ронять весь отчёт
            _log_collector_error("metrics", "Не удалось снять системные метрики", exc)
            warnings.append(f"метрики системы недоступны: {exc}{_proc_hint(exc)}")
            unavailable.add("metrics")
            return None

    async def _collect_services(
        self, warnings: list[str], unavailable: set[str]
    ) -> list[ServiceStatus]:
        try:
            return await self.services.collect()
        except Exception as exc:
            _log_collector_error(
                "services", "Не удалось прочитать состояние сервисов", exc
            )
            warnings.append(f"состояние сервисов недоступно: {exc}")
            unavailable.add("services")
            return []

    async def _collect_panel_health(
        self, warnings: list[str], unavailable: set[str]
    ) -> PanelHealth:
        if not self.settings.panel_enabled:
            return PanelHealth(
                reachable=False,
                status_code=None,
                latency_ms=None,
                error=None,
                disabled=True,
            )
        try:
            return await self.panel.health()
        except Exception as exc:
            _log_collector_error("panel", "Проверка панели упала", exc)
            warnings.append(f"проверка панели упала: {exc}")
            unavailable.add("panel")
            return PanelHealth(
                reachable=False, status_code=None, latency_ms=None, error=str(exc)
            )

    async def _collect_subscriptions(
        self, warnings: list[str], unavailable: set[str]
    ) -> tuple[list[InboundSummary], list[Subscription]]:
        if not self.settings.panel_enabled:
            return [], []
        try:
            inbounds, subs = await self.subscriptions.collect()
        except Exception as exc:  # сборщик не должен ронять весь срез
            _log_collector_error("subs", "Не удалось собрать подписки", exc)
            warnings.append(f"подписки не собраны: {exc}")
            unavailable.add("subs")
            return [], []
        if self.subscriptions.forbidden:
            if self.settings.panel_type == "3x-ui":
                warnings.append(
                    "список inbound'ов недоступен: у токена нет области "
                    f"node-sync/admin ({self.subscriptions.last_error})"
                )
            else:
                warnings.append(
                    f"API панели {self.settings.panel_type} отвергла запрос: "
                    f"{self.subscriptions.last_error}"
                )
        elif self.subscriptions.last_error:
            warnings.append(f"подписки не собраны: {self.subscriptions.last_error}")
        return inbounds, subs

    # ------------------------------------------------------------- действия
