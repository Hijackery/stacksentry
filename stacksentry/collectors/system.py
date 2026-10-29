"""Системные метрики локального сервера через psutil."""

from __future__ import annotations

import os
import time
from typing import Any

import psutil

from ..models import SystemMetrics

#: Сколько секунд ждём замер CPU: без интервала первое значение — среднее
#: с момента загрузки, а не текущая нагрузка.
CPU_SAMPLE_SECONDS = 0.3


class SystemCollector:
    """Снимает метрики CPU/RAM/диска/сети. Первый вызов прогревает счётчики сети."""

    def __init__(self, disk_path: str = "/") -> None:
        self.disk_path = disk_path
        self._cores = max(1, psutil.cpu_count() or 1)
        self._prev_net: Any = None
        self._prev_net_time: float = 0.0

    @staticmethod
    def load1() -> float:
        try:
            return os.getloadavg()[0]
        except (OSError, AttributeError):
            # В контейнерах без /proc/loadavg getloadavg может отсутствовать.
            return 0.0

    def collect(self) -> SystemMetrics:
        # cpu_percent(percpu=False) уже отдаёт загрузку всей системы в 0..100.
        # Делить на число ядер нельзя: на 8 ядрах 100% превращались в 12.5%,
        # и алерты CPU_WARN/CPU_CRIT не срабатывали никогда.
        cpu_percent = min(100.0, psutil.cpu_percent(interval=CPU_SAMPLE_SECONDS))
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            disk = psutil.disk_usage(self.disk_path)
        except (OSError, psutil.Error):
            # Несмонтированный DISK_PATH не должен обнулять CPU/RAM/сеть.
            try:
                disk = psutil.disk_usage("/")
            except (OSError, psutil.Error):
                disk = None
        try:
            net = psutil.net_io_counters()
        except (OSError, psutil.Error):
            net = None
        inode_percent = 0.0
        inode_free = 0
        try:
            stats = os.statvfs(self.disk_path)
        except OSError:
            try:
                stats = os.statvfs("/")
            except OSError:
                stats = None
        if stats is not None and stats.f_files:
            inode_free = stats.f_ffree
            inode_percent = (1 - stats.f_ffree / stats.f_files) * 100
        now = time.time()

        up_bps, down_bps = 0.0, 0.0
        if self._prev_net is not None and net is not None:
            elapsed = now - self._prev_net_time
            if elapsed > 0:
                up_bps = max(
                    0.0, (net.bytes_sent - self._prev_net.bytes_sent) / elapsed
                )
                down_bps = max(
                    0.0, (net.bytes_recv - self._prev_net.bytes_recv) / elapsed
                )

        self._prev_net = net
        self._prev_net_time = now

        connections = 0
        try:
            connections = len(psutil.net_connections(kind="tcp"))
        except (psutil.Error, OSError, PermissionError, RuntimeError):
            # net_connections требует CAP_NET_ADMIN; на VDS обычно доступно,
            # но в контейнерах может быть запрещено — это не повод ронять сбор.
            connections = -1

        return SystemMetrics(
            cpu_percent=round(cpu_percent, 1),
            mem_percent=round(virtual.percent, 1),
            mem_total_bytes=virtual.total,
            swap_percent=round(swap.percent, 1),
            disk_percent=round(disk.percent, 1) if disk else 0.0,
            disk_free_bytes=disk.free if disk else 0,
            load1=round(self.load1(), 2),
            uptime_seconds=time.time() - psutil.boot_time(),
            tcp_connections=connections,
            net_sent_bytes=net.bytes_sent if net else 0,
            net_recv_bytes=net.bytes_recv if net else 0,
            net_up_bps=round(up_bps, 1),
            net_down_bps=round(down_bps, 1),
            inode_percent=round(inode_percent, 1),
            inode_free=inode_free,
            collected_at=now,
        )
