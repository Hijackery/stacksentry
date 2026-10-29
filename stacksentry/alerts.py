"""Пороговые алерты с гистерезисом и кулдауном.

Гистерезис: после срабатывания порога состояние держится, пока значение не
опустится ниже порога на ``ALERT_HYSTERESIS`` процентов. Это защищает от
«дребезга» у границы, когда метрика колеблется вокруг порога.

Состояние правил живёт в SQLite, поэтому переживает перезапуск бота и
перезагрузку сервера: после ребута алерт не придёт заново, пока проблема
реально не ушла.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from .hints import panel_token_hint
from .models import MonitorSnapshot, Severity, SystemMetrics
from .settings import Settings
from .storage import Storage


class Rule(str, Enum):
    cpu = "cpu"
    mem = "mem"
    disk = "disk"
    swap = "swap"
    service = "service"
    panel = "panel"
    system = "system"
    inode = "inode"
    check = "check"


def _is_subscription_warning(text: str) -> bool:
    """Предупреждение о inbound'ах/подписках, а не о железе."""
    lowered = text.lower()
    return any(marker in lowered for marker in ("подписк", "inbound", "node-sync"))


@dataclass(frozen=True, slots=True)
class Finding:
    """Сработавшее условие, готовое к отправке в Telegram.

    ``key`` — под которым состояние правила хранится в SQLite. Он не должен
    содержать меняющихся значений (процентов, номеров), иначе после перезапуска
    бот забудет о старом алерте. Для метрик ключ совпадает с правилом, для
    панели и сервисов добавляется предмет (``panel:xray``, ``service:x-ui``).
    """

    rule: Rule
    severity: Severity
    title: str
    detail: str
    key: str = ""
    resolve: str = ""

    @property
    def state_key(self) -> str:
        return self.key or self.rule.value


class AlertEngine:
    """Считает состояния правил и решает, что и когда отправлять."""

    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage

    def evaluate(self, snapshot: MonitorSnapshot) -> list[Finding]:
        findings = list(self._metrics(snapshot.system))
        findings.extend(self._services(snapshot))
        findings.extend(self._panel(snapshot))
        findings.extend(self._checks(snapshot))
        if "services" in snapshot.unavailable:
            findings.append(
                Finding(
                    Rule.system,
                    Severity.unknown,
                    "Состояние служб недоступно",
                    "systemctl не ответил; прошлые состояния не считаем снятыми",
                    key="services:unavailable",
                )
            )
        if "subs" in snapshot.unavailable:
            findings.append(
                Finding(
                    Rule.panel,
                    Severity.unknown,
                    "Подписки не собраны",
                    "панель вернула неожиданный ответ; прошлые состояния сохранены",
                    key="subs:unavailable",
                )
            )
        return findings

    # -------------------------------------------------------------- правила

    def _metrics(self, metrics: SystemMetrics | None) -> list[Finding]:
        if metrics is None:
            return [
                Finding(
                    Rule.cpu,
                    Severity.unknown,
                    "Системные метрики недоступны",
                    "Сбор psutil завершился ошибкой",
                    key="metrics",
                )
            ]

        checks = (
            (
                Rule.cpu,
                metrics.cpu_percent,
                self.settings.cpu_warn,
                self.settings.cpu_crit,
                "CPU",
            ),
            (
                Rule.mem,
                metrics.mem_percent,
                self.settings.mem_warn,
                self.settings.mem_crit,
                "RAM",
            ),
            (
                Rule.disk,
                metrics.disk_percent,
                self.settings.disk_warn,
                self.settings.disk_crit,
                f"Диск {self.settings.disk_path}",
            ),
            (Rule.swap, metrics.swap_percent, 50.0, 80.0, "Swap"),
            (
                Rule.inode,
                metrics.inode_percent,
                self.settings.inode_warn,
                self.settings.inode_crit,
                "Inode",
            ),
        )
        findings = [finding for finding in map(self._threshold, checks) if finding]
        forecast = metrics.disk_forecast_days
        if forecast is not None and forecast <= 7:
            severity = Severity.critical if forecast <= 2 else Severity.warning
            findings.append(
                Finding(
                    Rule.disk,
                    severity,
                    f"Диск заполнится примерно через {forecast:.0f} дн.",
                    f"текущее заполнение {metrics.disk_percent:.1f}%",
                    key="disk:forecast",
                    resolve="Освободите место или увеличьте диск",
                )
            )
        return findings

    def _checks(self, snapshot: MonitorSnapshot) -> list[Finding]:
        return [
            Finding(
                Rule.check,
                check.severity,
                check.title,
                check.detail,
                key=check.key,
                resolve=check.resolve,
            )
            for check in snapshot.checks
        ]

    def _threshold(
        self,
        check: tuple[Rule, float, float, float, str],
    ) -> Finding | None:
        rule, value, warn, crit, title = check
        severity = self._severity_with_hysteresis(rule, value, warn, crit)
        if severity is Severity.ok:
            return None
        return Finding(
            rule,
            severity,
            f"{title}: {value:.1f}%",
            f"порог: warn {warn:.0f}%, crit {crit:.0f}%",
            key=rule.value,
        )

    def _severity_with_hysteresis(
        self, rule: Rule, value: float, warn: float, crit: float
    ) -> Severity:
        """Состояние с гистерезисом: упасть с порога трудно, вырасти легко.

        Порядок проверок важен: сначала удержание уже достигнутого уровня,
        и только потом «сырые» пороги. Иначе ветка ``value >= warn``
        перехватывала бы всё, что ниже crit, и удержание crit не работало бы
        никогда.
        """
        previous = self._stored_severity(rule)
        hyst = self.settings.alert_hysteresis
        if value >= crit:
            return Severity.critical
        if previous is Severity.critical and value >= crit - hyst:
            return Severity.critical
        if value >= warn:
            return Severity.warning
        if previous is Severity.warning and value >= warn - hyst:
            return Severity.warning
        return Severity.ok

    def _services(self, snapshot: MonitorSnapshot) -> list[Finding]:
        findings: list[Finding] = []
        for service in snapshot.services:
            if service.active:
                continue
            detail = f"ActiveState={service.active_state}, SubState={service.sub_state}"
            if service.error:
                detail = f"{detail}; {service.error}"
            findings.append(
                Finding(
                    Rule.service,
                    Severity.critical,
                    f"Сервис {service.name} не работает",
                    detail,
                    key=f"service:{service.name}",
                    resolve="Кнопка «Рестарт» в разделе Сервисы",
                )
            )
        return findings

    def _panel(self, snapshot: MonitorSnapshot) -> list[Finding]:
        findings: list[Finding] = []
        panel = snapshot.panel
        if panel.disabled:
            return findings

        if not panel.reachable:
            findings.append(
                Finding(
                    Rule.panel,
                    Severity.critical,
                    "Панель 3X-UI недоступна",
                    panel.error or "нет ответа от панели",
                    key="panel:unreachable",
                    resolve="Проверьте systemctl status x-ui и порт панели",
                )
            )
            return findings

        if panel.xray_state and panel.xray_state != "running":
            findings.append(
                Finding(
                    Rule.panel,
                    Severity.critical,
                    "Xray не запущен",
                    f"состояние={panel.xray_state}, версия={panel.xray_version or '?'}",
                    key="panel:xray",
                    resolve="Кнопка «Рестарт xray» в разделе Сервисы",
                )
            )
        if not panel.api_authorized:
            detail = (
                "панель отвечает, но токен не принят. Где взять: "
                + panel_token_hint(panel.panel_version or "")
            )
            if any("404" in item for item in snapshot.warnings):
                detail += (
                    "\n→ 404 на inbounds/list: неверный PANEL_BASE_PATH "
                    "(webBasePath панели), адрес панели или просроченный токен"
                )
            findings.append(
                Finding(
                    Rule.panel,
                    Severity.warning,
                    "API панели не авторизует бота",
                    detail,
                    key="panel:api",
                )
            )

        sub_warnings = [
            item for item in snapshot.warnings if _is_subscription_warning(item)
        ]
        if sub_warnings:
            findings.append(
                Finding(
                    Rule.panel,
                    Severity.warning,
                    "Подписки: нужна проверка",
                    "\n".join(f"• {item}" for item in sub_warnings),
                    key="panel:subscriptions",
                )
            )
        return findings

    # ------------------------------------------------------------ состояния

    def update(
        self,
        findings: list[Finding],
        *,
        unavailable: frozenset[str] = frozenset(),
    ) -> list[Finding]:
        """Обновляет состояния правил и возвращает то, что нужно отправить.

        Отправка происходит при смене состояния (в том числе при восстановлении)
        и повторно, если проблема держится дольше ``ALERT_COOLDOWN``. Отметка
        «отправлено» ставится отдельно — ``mark_sent`` — чтобы не потерять
        алерт, если отправка в Telegram не удалась.
        """
        now = time.time()
        self.storage.prune_mutes()
        every = {finding.state_key: finding for finding in findings}
        known = (
            every.keys()
            | set(self.storage.all_alert_keys())
            | set(self.storage.all_alert_candidate_keys())
        )
        # Заглушенные («знаю, до утра») не участвуют вообще: иначе снятый мьют
        # прилетел бы как «восстановлено», хотя проблема не уходила.
        muted = {key for key in known if self.storage.is_muted(key)}
        active = {k: v for k, v in every.items() if k not in muted}
        rules = set(known) - muted

        outbox: list[Finding] = []
        for key in sorted(rules):
            previous = self.storage.get_alert_state(key)
            old_state = Severity(previous["state"]) if previous else Severity.ok
            finding = active.get(key)
            # Источник не опрошен — это не «восстановлено»: состояние держим.
            if finding is None and _source_of(key) in unavailable:
                continue
            new_state = finding.severity if finding else Severity.ok

            if new_state is old_state:
                self.storage.clear_alert_candidate(key)
                if new_state is Severity.ok or finding is None:
                    continue
                last = _as_float(previous["notified_at"] if previous else None)
                if last and now - last < self.settings.alert_cooldown:
                    continue
                outbox.append(finding)
                continue

            # Переход подтверждаем несколькими замерами подряд: одиночный
            # всплеск CPU, таймаут панели или короткий рестарт службы не
            # должны выглядеть как авария. Восстановление тоже дебаунсим,
            # чтобы не было качелей «упало/восстановилось».
            required = (
                self.settings.alert_confirm_checks
                if new_state is not Severity.ok
                else self.settings.alert_recovery_checks
            )
            candidate = self.storage.get_alert_candidate(key)
            if candidate is None or candidate["state"] != new_state.value:
                seen = 1
                first_seen = now
            else:
                seen = int(candidate["seen"]) + 1
                first_seen = float(candidate["first_seen"])
            if seen < required:
                self.storage.set_alert_candidate(
                    key,
                    new_state.value,
                    seen=seen,
                    first_seen=first_seen,
                )
                continue
            self.storage.clear_alert_candidate(key)
            self.storage.set_alert_state(key, new_state.value, changed_at=now)
            if new_state is Severity.ok:
                outbox.append(
                    Finding(
                        Rule(key.split(":")[0]) if _is_rule(key) else Rule.panel,
                        Severity.ok,
                        f"{key}: восстановлено",
                        f"уровень «{old_state.value}» больше не выполняется",
                        key=key,
                    )
                )
            elif finding is not None:
                outbox.append(finding)
        return outbox

    def mark_sent(self, keys: list[str], *, at: float | None = None) -> None:
        """Отмечает состояния как отправленные — до этого они шлются снова."""
        now = at if at is not None else time.time()
        for key in keys:
            self.storage.mark_alert_sent(key, notified_at=now)

    def _stored_severity(self, rule: Rule) -> Severity:
        previous = self.storage.get_alert_state(rule.value)
        return Severity(previous["state"]) if previous else Severity.ok


def _is_rule(key: str) -> bool:
    head = key.split(":")[0]
    return any(head == member.value for member in Rule)


def _source_of(key: str) -> str:
    """К какому сборщику относится ключ состояния."""
    if key.startswith("service:"):
        return "services"
    if key.startswith("panel:"):
        return "panel"
    if key.startswith("check:"):
        return "checks"
    if key.startswith("subs") or key.startswith("subscription"):
        return "subs"
    return "metrics"


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
