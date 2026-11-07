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
        cores = psutil.cpu_percent(interval=CPU_SAMPLE_SECONDS, percpu=True)
        cores = tuple(min(100.0, float(value)) for value in cores) if cores else ()
        cpu_percent = sum(cores) / len(cores) if cores else 0.0
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            freq = psutil.cpu_freq()
        except (OSError, psutil.Error):
            freq = None
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

        try:
            load1, load5, load15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = 0.0

        return SystemMetrics(
            cpu_percent=round(cpu_percent, 1),
            mem_percent=round(virtual.percent, 1),
            mem_total_bytes=virtual.total,
            swap_percent=round(swap.percent, 1),
            disk_percent=round(disk.percent, 1) if disk else 0.0,
            disk_free_bytes=disk.free if disk else 0,
            load1=round(load1, 2),
            load5=round(load5, 2),
            load15=round(load15, 2),
            uptime_seconds=time.time() - psutil.boot_time(),
            tcp_connections=connections,
            net_sent_bytes=net.bytes_sent if net else 0,
            net_recv_bytes=net.bytes_recv if net else 0,
            net_up_bps=round(up_bps, 1),
            net_down_bps=round(down_bps, 1),
            inode_percent=round(inode_percent, 1),
            inode_free=inode_free,
            cpu_cores=tuple(round(value, 1) for value in cores),
            cpu_count=len(cores) or (psutil.cpu_count() or 0),
            cpu_freq_mhz=round(freq.current, 0) if freq else 0.0,
            mem_available_bytes=virtual.available,
            swap_total_bytes=swap.total,
            swap_used_bytes=swap.used,
            disk_total_bytes=disk.total if disk else 0,
            disk_used_bytes=disk.used if disk else 0,
            collected_at=now,
        )
