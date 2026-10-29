"""Модели данных мониторинга."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ok = "ok"
    warning = "warning"
    critical = "critical"
    unknown = "unknown"


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    cpu_percent: float
    mem_percent: float
    mem_total_bytes: int
    swap_percent: float
    disk_percent: float
    disk_free_bytes: int
    load1: float
    uptime_seconds: float
    tcp_connections: int
    net_sent_bytes: int
    net_recv_bytes: int
    net_up_bps: float = 0.0
    net_down_bps: float = 0.0
    inode_percent: float = 0.0
    inode_free: int = 0
    disk_forecast_days: float | None = None
    collected_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    name: str
    active: bool
    sub_state: str
    active_state: str
    main_pid: int
    since: str
    restarts: int
    memory_bytes: int
    cpu_percent: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PanelHealth:
    reachable: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None
    panel_version: str = ""
    xray_version: str = ""
    xray_state: str = ""
    api_authorized: bool = False
    #: API панели сознательно не настроен (monitor-only).
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class Subscription:
    email: str
    inbound_remark: str
    enabled: bool
    expiry_time_ms: int
    # 3X-UI называет поле totalGB, но хранит и отдаёт БАЙТЫ.
    total_bytes: int
    used_bytes: int
    sub_id: str = ""

    @property
    def is_expired(self) -> bool:
        return self.expiry_time_ms > 0 and self.expiry_time_ms <= int(
            time.time() * 1000
        )

    @property
    def expires_in_days(self) -> float | None:
        if self.expiry_time_ms <= 0:
            return None
        return (self.expiry_time_ms - time.time() * 1000) / 86_400_000

    @property
    def is_depleted(self) -> bool:
        return self.total_bytes > 0 and self.used_bytes >= self.total_bytes

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, self.used_bytes / self.total_bytes * 100)


@dataclass(frozen=True, slots=True)
class InboundSummary:
    remark: str
    port: int
    protocol: str
    enabled: bool
    clients: int
    up_bytes: int
    down_bytes: int
    total_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.up_bytes + self.down_bytes


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Результат дополнительной проверки: SMART, TLS, бэкап, HTTP/DNS."""

    key: str
    severity: Severity
    title: str
    detail: str
    resolve: str = ""


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    """Полный срез состояния сервера и панели."""

    system: SystemMetrics | None
    services: list[ServiceStatus]
    panel: PanelHealth
    inbounds: list[InboundSummary]
    subscriptions: list[Subscription]
    checks: list[HealthCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Источники, которые в этом срезе не удалось опросить. Их прошлые
    #: состояния нельзя объявлять «восстановленными».
    unavailable: frozenset[str] = field(default_factory=frozenset)

    @property
    def total_used_bytes(self) -> int:
        return sum(item.used_bytes for item in self.inbounds)

    @property
    def active_clients(self) -> int:
        return sum(item.clients for item in self.inbounds if item.enabled)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Результат выполнения привилегированной команды."""

    action: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def summary(self, limit: int = 3000) -> str:
        parts: list[str] = []
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(f"stderr:\n{self.stderr.strip()}")
        text = "\n".join(parts) or "(пустой вывод)"
        if limit > 0 and len(text) > limit:
            text = text[-limit:]
        return text
