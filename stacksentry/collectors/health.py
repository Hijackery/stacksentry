"""Дополнительные проверки: failed units, TLS, бэкапы, HTTP/DNS и SMART."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..models import HealthCheck, Severity
from ..settings import Settings

logger = logging.getLogger(__name__)

_TLS_WARN_DAYS = 14
_TLS_CRIT_DAYS = 3


class HealthCollector:
    """Собирает проверки, которым не место в основном снимке метрик."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def collect(self) -> list[HealthCheck]:
        tasks: list[asyncio.Future[HealthCheck | None]] = []
        if self.settings.check_failed_units:
            tasks.append(asyncio.create_task(self._failed_units()))
        if self.settings.check_tls and self.settings.panel_url:
            tasks.append(asyncio.create_task(self._tls()))
        if self.settings.check_backup_path:
            tasks.append(asyncio.create_task(self._backup()))
        for index, url in enumerate(self.settings.check_http_urls):
            tasks.append(asyncio.create_task(self._http(index, url)))
        for host in self.settings.check_dns_hosts:
            tasks.append(asyncio.create_task(self._dns(host)))
        if self.settings.check_smart:
            tasks.append(asyncio.create_task(self._smart()))
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        checks: list[HealthCheck] = []
        for result in results:
            if isinstance(result, HealthCheck):
                checks.append(result)
            elif isinstance(result, Exception):
                logger.debug("Дополнительная проверка упала: %s", result)
        return checks

    async def _failed_units(self) -> HealthCheck | None:
        def run() -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    ["systemctl", "--failed", "--no-legend", "--plain"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None

        proc = await asyncio.to_thread(run)
        if proc is None or proc.returncode != 0:
            return None
        ignored = set(self.settings.check_failed_units_ignore)
        units = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] not in ignored:
                units.append(parts[0])
        if not units:
            return None
        return HealthCheck(
            "check:failed_units",
            Severity.warning,
            "Есть упавшие службы",
            ", ".join(units[:10]),
            "Откройте «Сервисы» или выполните systemctl --failed",
        )

    async def _tls(self) -> HealthCheck | None:
        parts = urlsplit(self.settings.panel_url)
        if parts.scheme != "https" or not parts.hostname:
            return None
        host = parts.hostname
        port = parts.port or 443

        def run() -> float | None:
            context = ssl.create_default_context()
            try:
                with (
                    socket.create_connection((host, port), timeout=5) as raw,
                    context.wrap_socket(raw, server_hostname=host) as tls,
                ):
                    cert = tls.getpeercert()
            except (OSError, ssl.SSLError, ValueError):
                return None
            if not cert:
                return None
            expires = cert.get("notAfter")
            if not isinstance(expires, str):
                return None
            try:
                return (ssl.cert_time_to_seconds(expires) - time.time()) / 86400
            except (TypeError, ValueError):
                return None

        days = await asyncio.to_thread(run)
        if days is None:
            return None
        if days <= _TLS_CRIT_DAYS:
            severity = Severity.critical
        elif days <= _TLS_WARN_DAYS:
            severity = Severity.warning
        else:
            return None
        return HealthCheck(
            "check:tls",
            severity,
            f"TLS-сертификат панели истекает через {days:.0f} дн.",
            f"{host}:{port}",
            "Обновите сертификат или проверьте автопродление",
        )

    async def _backup(self) -> HealthCheck | None:
        path = Path(self.settings.check_backup_path)
        max_age = self.settings.check_backup_max_age_h

        def newest_mtime() -> float | None:
            try:
                if path.is_file():
                    return path.stat().st_mtime
                if path.is_dir():
                    mtimes = [item.stat().st_mtime for item in path.iterdir()]
                    return max(mtimes) if mtimes else None
            except OSError:
                return None
            return None

        mtime = await asyncio.to_thread(newest_mtime)
        if mtime is None:
            return HealthCheck(
                "check:backup",
                Severity.warning,
                "Бэкап не найден",
                str(path),
                "Проверьте путь CHECK_BACKUP_PATH и задание бэкапа",
            )
        age_h = (time.time() - mtime) / 3600
        if age_h <= max_age:
            return None
        severity = Severity.critical if age_h > max_age * 2 else Severity.warning
        return HealthCheck(
            "check:backup",
            severity,
            "Бэкап устарел",
            f"{path}: последнее изменение {age_h:.1f} ч назад",
            f"Порог CHECK_BACKUP_MAX_AGE_H={max_age}",
        )

    async def _http(self, index: int, url: str) -> HealthCheck | None:
        host = urlsplit(url).hostname or f"url{index}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(6.0), follow_redirects=True
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            return HealthCheck(
                f"check:http:{index}:{host}",
                Severity.warning,
                f"HTTP {host} недоступен",
                str(exc) or exc.__class__.__name__,
                "Проверьте адрес CHECK_HTTP_URLS и сеть",
            )
        if response.status_code < 400:
            return None
        return HealthCheck(
            f"check:http:{index}:{host}",
            Severity.warning,
            f"HTTP {host}: код {response.status_code}",
            url,
            "Проверьте сервис и адрес CHECK_HTTP_URLS",
        )

    async def _dns(self, host: str) -> HealthCheck | None:
        try:
            await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError as exc:
            return HealthCheck(
                f"check:dns:{host}",
                Severity.warning,
                f"DNS не разрешает {host}",
                str(exc),
                "Проверьте DNS-сервер и имя CHECK_DNS_HOSTS",
            )
        return None

    async def _smart(self) -> HealthCheck | None:
        def scan() -> list[str]:
            try:
                proc = subprocess.run(
                    ["smartctl", "--scan", "-H"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return []
            devices = []
            for line in proc.stdout.splitlines():
                parts = line.split()
                if parts and parts[0].startswith("/dev/"):
                    devices.append(parts[0])
            return devices

        devices = await asyncio.to_thread(scan)
        if not devices:
            return HealthCheck(
                "check:smart",
                Severity.unknown,
                "SMART недоступен",
                "smartctl не найден или не видит диски",
                "Установите smartmontools или отключите CHECK_SMART",
            )

        def health(device: str) -> str:
            try:
                proc = subprocess.run(
                    ["smartctl", "-H", device],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return "unknown"
            text = (proc.stdout + proc.stderr).upper()
            if "FAILED" in text:
                return "failed"
            if "PASSED" in text or "OK" in text:
                return "ok"
            return "unknown"

        results = await asyncio.gather(
            *(asyncio.to_thread(health, device) for device in devices)
        )
        failed = [
            device
            for device, state in zip(devices, results, strict=True)
            if state == "failed"
        ]
        if failed:
            return HealthCheck(
                "check:smart",
                Severity.critical,
                "SMART: диск сообщает об ошибке",
                ", ".join(failed),
                "Срочно проверьте диск и бэкапы",
            )
        return None
