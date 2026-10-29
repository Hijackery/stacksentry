"""Текстовые экраны и отчёты StackSentry."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from .alerts import Finding
from .hints import panel_token_hint
from .inventory import Detected
from .models import (
    InboundSummary,
    MonitorSnapshot,
    ServiceStatus,
    Subscription,
    SystemMetrics,
)
from .settings import Settings
from .ui_common import (
    _COMMAND_GROUPS,
    _STACK_TITLES,
    COMMAND_HELP,
    MAX_INLINE_ROWS,
    PACKAGE_MANAGER_TITLES,
    PANEL_RESTART,
    SUBSCRIPTIONS_PAGE,
    esc,
    fmt_bytes,
    fmt_uptime,
    level,
    panel_title,
    progress_bar,
    severity_icon,
)
from .updates import UpdateInfo


def render_overview(
    snapshot: MonitorSnapshot,
    settings: Settings,
    *,
    age: float | None = None,
    compact: bool = False,
) -> str:
    """Главный отчёт: вердикт в первой строке, дальше детали.

    Уставшему админу нужен ответ «всё ок или нет» сразу, а не после чтения
    двадцати строк, поэтому состояние вынесено в заголовок, а замечания — выше
    подробностей.
    """
    metrics = snapshot.system
    # Без повторов: одна причина — одна строка.
    problems = list(dict.fromkeys(snapshot.warnings))
    if snapshot.panel.reachable is False and not snapshot.panel.disabled:
        problems.append("панель недоступна")
    if metrics is None:
        # Без метрик вердикт «всё в норме» был бы враньём.
        problems.append("метрики системы недоступны")
    down = [item.name for item in snapshot.services if not item.active]

    if down:
        verdict = "сервисы не работают: " + ", ".join(down)
    elif problems:
        verdict = f"есть замечания: {len(problems)}"
    else:
        verdict = "всё в норме"

    bits: list[str] = []
    if metrics is not None:
        bits.extend(
            (
                f"CPU {metrics.cpu_percent:.0f}%",
                f"RAM {metrics.mem_percent:.0f}%",
                f"Диск {metrics.disk_percent:.0f}%",
            )
        )
    services_bit = " · ".join(
        f"{item.name} {'🟢' if item.active else '🔴'}" for item in snapshot.services
    )
    if services_bit:
        bits.append(services_bit)
    if snapshot.panel.disabled:
        bits.append("панель ⚪")
    elif snapshot.panel.reachable:
        bits.append("панель 🟢")
    elif snapshot.panel.reachable is False:
        bits.append("панель 🔴")

    lines = [f"🖥 <b>Состояние сервера: {verdict}</b>"]
    if bits:
        lines.append(" · ".join(bits))

    if problems:
        lines.extend(("", "━━━ <b>Требует внимания</b>"))
        lines.extend(f"• {esc(item)}" for item in problems)

    if compact:
        # Ночью нужен один экран: вердикт и проблемы. Детали — по кнопке.
        lines.append("")
        lines.append(_stamp_line(age))
        return "\n".join(lines)

    lines.append("")
    lines.append(
        render_system(metrics, settings)
        if metrics is not None
        else "Метрики системы недоступны"
    )
    lines.append(render_services(snapshot))
    lines.append(render_panel(snapshot, panel_type=settings.panel_type))
    lines.append(render_traffic(snapshot, panel_type=settings.panel_type))

    lines.append(_stamp_line(age))
    return "\n".join(lines)


def _stamp_line(age: float | None) -> str:
    """«данные на 03:12 (12 с назад) · время сервера» — без догадок о поясе."""
    suffix = f" ({age:.0f} с назад)" if age is not None and age >= 1 else ""
    return f"\n<i>данные на {time.strftime('%H:%M:%S')}{suffix} · время сервера</i>"


def render_system(metrics: SystemMetrics, settings: Settings) -> str:
    """Метрики одним выровненным блоком.

    Моноширинный шрифт держит колонки ровными при любых значениях — иначе
    полосы и подписи «едут» и цифры приходится искать глазами.
    """
    tcp = metrics.tcp_connections if metrics.tcp_connections >= 0 else "н/д"
    rows = (
        ("CPU ", metrics.cpu_percent, settings.cpu_warn, settings.cpu_crit),
        ("RAM ", metrics.mem_percent, settings.mem_warn, settings.mem_crit),
        ("Диск", metrics.disk_percent, settings.disk_warn, settings.disk_crit),
    )
    lines: list[str] = []
    for label, percent, warn, crit in rows:
        icon = severity_icon(level(percent, warn, crit))
        lines.append(f"{label} {progress_bar(percent)} {percent:5.1f}% {icon}")

    used_mem = metrics.mem_total_bytes * metrics.mem_percent / 100
    lines.append(
        f"Память {fmt_bytes(used_mem)} занято из {fmt_bytes(metrics.mem_total_bytes)}"
    )
    lines.append(f"Свободно на диске: {fmt_bytes(metrics.disk_free_bytes)}")
    lines.append(
        f"Swap {metrics.swap_percent:.1f}% · Load {metrics.load1:.2f} · TCP {tcp}"
    )
    if metrics.inode_percent > 0:
        lines.append(f"Inode {metrics.inode_percent:.1f}%")
    if metrics.disk_forecast_days is not None:
        lines.append(
            f"Прогноз диска: ~{metrics.disk_forecast_days:.0f} дн. до заполнения"
        )
    lines.append(
        f"Сеть ↑ {fmt_bytes(metrics.net_sent_bytes)} / "
        f"↓ {fmt_bytes(metrics.net_recv_bytes)}"
    )
    lines.append(f"Аптайм {fmt_uptime(metrics.uptime_seconds)}")
    return "<code>" + "\n".join(lines) + "</code>"


def render_stack(
    detected: list[Detected],
    watched: tuple[str, ...],
    services: list[ServiceStatus],
) -> str:
    """Что найдено на сервере и что из этого реально наблюдается."""
    if not detected:
        return (
            "\n🧩 <b>Стек сервера</b>\n"
            "Панели и VPN-службы не найдены. Проверьте, что они установлены, "
            "или добавьте службы вручную: <code>sudo stacksentry-menu</code>."
        )
    active = {item.name: item.active for item in services}
    lines = [
        "",
        "🧩 <b>Стек сервера</b>",
        "Найдено на сервере; 🟢/🔴 — наблюдается и работает/лежит, "
        "⚪ — не наблюдается.",
    ]
    covered: set[str] = set()
    for kind, title in _STACK_TITLES.items():
        items = [item for item in detected if item.item.kind == kind]
        if not items:
            continue
        lines += ["", f"<b>{title}</b>"]
        for found in items:
            unit = found.unit
            if unit:
                covered.add(unit)
            if unit and unit in watched:
                icon = "🟢" if active.get(unit, False) else "🔴"
                status = "наблюдается"
            else:
                icon = "⚪"
                status = "не наблюдается"
            suffix = f" · <code>{esc(unit)}</code>" if unit else ""
            lines.append(f"{icon} {esc(found.item.title)}{suffix} — {status}")
            if found.item.note:
                lines.append(f"   <i>{esc(found.item.note)}</i>")
    extras = [unit for unit in watched if unit not in covered]
    if extras:
        lines += ["", "<b>Другие службы</b>"]
        for unit in extras:
            icon = "🟢" if active.get(unit, False) else "🔴"
            lines.append(f"{icon} <code>{esc(unit)}</code>")
    lines += [
        "",
        "Добавить в наблюдение: <code>sudo stacksentry-menu</code> → "
        "«Службы» (пункт 16).",
    ]
    return "\n".join(lines)


def render_services(snapshot: MonitorSnapshot) -> str:
    if not snapshot.services:
        return "\n⚙️ <b>Сервисы</b>\nне удалось прочитать состояние"
    lines = ["", "⚙️ <b>Сервисы</b>"]
    for service in snapshot.services:
        icon = "🟢" if service.active else "🔴"
        state = "работает" if service.active else esc(service.active_state)
        restarts = (
            f" · перезапускался {service.restarts} раз" if service.restarts else ""
        )
        lines.append(
            f"{icon} <code>{esc(service.name)}</code> — {state}"
            f" · RAM {fmt_bytes(service.memory_bytes)}"
            f" · CPU {service.cpu_percent:.1f}%{restarts}"
        )
    return "\n".join(lines)


def render_panel(snapshot: MonitorSnapshot, panel_type: str = "3x-ui") -> str:
    panel = snapshot.panel
    name = panel_title(panel_type)
    if panel.disabled:
        return (
            f"\n🌐 <b>Панель {esc(name)}</b>\n"
            "⚪ API панели не настроен — режим «только мониторинг служб».\n\n"
            "Службы и VPN-стек наблюдаются; пользователи и трафик недоступны.\n"
            "Настроить API: <code>sudo stacksentry-menu</code> → "
            "тип, адрес и токен панели."
        )
    if not panel.reachable:
        reason = esc(panel.error or "нет ответа")
        return f"\n🌐 <b>Панель {esc(name)}</b>\n🔴 недоступна: {reason}"
    latency = f"{panel.latency_ms:.0f} мс" if panel.latency_ms is not None else "—"
    api = "токен принят" if panel.api_authorized else "⚠️ токен не принят"
    core = ""
    if panel_type == "3x-ui":
        core = (
            f" · xray {esc(panel.xray_version or '?')} ({esc(panel.xray_state or '?')})"
        )
    hint = ""
    if not panel.api_authorized:
        if panel_type == "3x-ui":
            hint = (
                f"\n→ токен панели не принят. Где взять: "
                f"{panel_token_hint(panel.panel_version or '')}"
                "\n→ проверьте PANEL_BASE_PATH (webBasePath панели) и срок "
                "действия токена: иначе API отвечает 404/401"
            )
        else:
            hint = (
                "\n→ проверьте PANEL_TYPE, PANEL_URL (включая путь панели) "
                "и PANEL_API_TOKEN в <code>sudo stacksentry-menu</code>"
            )
    return (
        f"\n🌐 <b>Панель {esc(name)}</b>\n"
        f"🟢 отвечает за {latency}\n"
        f"Панель {esc(panel.panel_version or '?')}{core}\n"
        f"API: {api}{hint}"
    )


def render_traffic(snapshot: MonitorSnapshot, panel_type: str = "3x-ui") -> str:
    """Трафик: итоги по inbound'ам и топ клиентов (в отличие от «Подписок»)."""
    name = panel_title(panel_type)
    if not snapshot.inbounds and not snapshot.subscriptions:
        if snapshot.panel.disabled:
            return (
                "\n📊 <b>Трафик</b>\n"
                "API панели не настроен — трафик недоступен.\n"
                "Службы и VPN-стек наблюдаются. Настроить: "
                "<code>sudo stacksentry-menu</code>."
            )
        hints = (
            "• у API-токена панели scope <code>node-sync</code> или "
            "<code>admin</code>;\n"
            "• <code>PANEL_BASE_PATH</code> — это «URL панели» из настроек "
            "(обычно пусто), а не путь <code>/panel</code>;\n"
            "• раздел «🌐 Панель» — отвечает ли API и принят ли токен.\n\n"
            "Токен: «Настройки панели → Учётная запись → API-токен»."
            if panel_type == "3x-ui"
            else (
                "• тип панели (<code>PANEL_TYPE</code>) и адрес с её путём;\n"
                "• токен/логин в <code>sudo stacksentry-menu</code>;\n"
                "• раздел «🌐 Панель» — отвечает ли API и принят ли токен."
            )
        )
        return (
            "\n📊 <b>Трафик</b>\n"
            f"Панель {esc(name)} не отдала пользователей — показывать нечего.\n\n"
            "<b>Что проверить</b>\n"
            f"{hints}"
        )

    if snapshot.inbounds:
        lines = [
            "",
            "📊 <b>Трафик</b>",
            f"Включено {sum(1 for item in snapshot.inbounds if item.enabled)} из "
            f"{len(snapshot.inbounds)} · клиентов {snapshot.active_clients} · "
            f"суммарно {fmt_bytes(snapshot.total_used_bytes)}",
        ]
        for inbound in snapshot.inbounds[:MAX_INLINE_ROWS]:
            icon = "🟢" if inbound.enabled else "⚪"
            limit = (
                f" из {fmt_bytes(inbound.total_bytes)}" if inbound.total_bytes else ""
            )
            lines.append(
                f"{icon} {esc(inbound.remark)} — "
                f"{esc(inbound.protocol)}:{inbound.port} · "
                f"клиентов {inbound.clients} · ↑{fmt_bytes(inbound.up_bytes)} "
                f"↓{fmt_bytes(inbound.down_bytes)}{limit}"
            )
        if len(snapshot.inbounds) > MAX_INLINE_ROWS:
            lines.append(f"… ещё {len(snapshot.inbounds) - MAX_INLINE_ROWS}")
    else:
        used = sum(item.used_bytes for item in snapshot.subscriptions)
        active = sum(1 for item in snapshot.subscriptions if item.enabled)
        lines = [
            "",
            "📊 <b>Трафик</b>",
            f"Панель {esc(name)} · пользователей {len(snapshot.subscriptions)} "
            f"(активных {active}) · суммарно {fmt_bytes(used)}",
        ]

    top = sorted(
        (item for item in snapshot.subscriptions if item.used_bytes > 0),
        key=lambda item: item.used_bytes,
        reverse=True,
    )[:8]
    if top:
        lines += ["", "👥 <b>Топ клиентов по трафику</b>"]
        for item in top:
            limit = (
                f" из {fmt_bytes(item.total_bytes)} ({item.used_percent:.0f}%)"
                if item.total_bytes
                else ""
            )
            lines.append(
                f"• <code>{esc(item.email or item.sub_id or '—')}</code> — "
                f"{fmt_bytes(item.used_bytes)}{limit}"
            )
        if len(snapshot.subscriptions) > len(top):
            lines.append(f"… ещё {len(snapshot.subscriptions) - len(top)} с трафиком")

    return "\n".join(lines)


def ordered_subscriptions(
    subscriptions: list[Subscription], soon_days: int
) -> list[Subscription]:
    """Порядок показа: проблемные (истёкшие, исчерпанные, скоро истекают) — вперёд."""
    expired = [item for item in subscriptions if item.is_expired]
    depleted = [item for item in subscriptions if item.is_depleted]
    soon = [
        item
        for item in subscriptions
        if not item.is_expired
        and item.expires_in_days is not None
        and item.expires_in_days <= soon_days
    ]
    interesting = expired + depleted + soon
    seen: set[str] = set()
    ordered: list[Subscription] = []
    for item in interesting:
        if item.email not in seen:
            seen.add(item.email)
            ordered.append(item)
    ordered.extend(item for item in subscriptions if item.email not in seen)
    return ordered


def render_subscriptions(
    subscriptions: list[Subscription],
    *,
    soon_days: int = 7,
    limit: int = SUBSCRIPTIONS_PAGE,
    offset: int = 0,
) -> str:
    if not subscriptions:
        return "Подписок нет или список недоступен для этого токена."

    expired = [item for item in subscriptions if item.is_expired]
    depleted = [item for item in subscriptions if item.is_depleted]
    soon = [
        item
        for item in subscriptions
        if not item.is_expired
        and item.expires_in_days is not None
        and item.expires_in_days <= soon_days
    ]
    ordered = ordered_subscriptions(subscriptions, soon_days)
    page = ordered[offset : offset + limit]

    lines = [
        "🪪 <b>Подписки</b>",
        f"всего {len(subscriptions)} · истёкших {len(expired)} · "
        f"трафик исчерпан {len(depleted)} · истекают ≤{soon_days} дн. {len(soon)}",
    ]
    if offset > 0 or offset + len(page) < len(ordered):
        lines.append(
            f"страница {offset // limit + 1}: показано {offset + 1}–"
            f"{offset + len(page)} из {len(ordered)}"
        )
    lines.append("")
    lines.extend(_render_subscription(item) for item in page)
    if offset + len(page) < len(ordered):
        lines.append(f"\n… ещё {len(ordered) - offset - len(page)} — кнопка «📄 Ещё»")
    return "\n".join(lines)


def _render_subscription(item: Subscription) -> str:
    flags = []
    if item.is_expired:
        days = abs(int(item.expires_in_days)) if item.expires_in_days else None
        flags.append(f"❌ просрочена {days} дн. назад" if days else "❌ просрочена")
    if item.is_depleted:
        flags.append("🈳 трафик исчерпан")
    if not item.enabled:
        flags.append("⏸ выключена")
    if item.expires_in_days is not None and 0 < item.expires_in_days <= 7:
        flags.append(f"⌛ {item.expires_in_days:.1f} дн.")

    traffic = fmt_bytes(item.used_bytes)
    if item.total_bytes:
        traffic += f" из {fmt_bytes(item.total_bytes)} ({item.used_percent:.0f}%)"

    suffix = f" · {' · '.join(flags)}" if flags else ""
    return (
        f"• <code>{esc(item.email)}</code> [{esc(item.inbound_remark)}]\n"
        f"   <code>{traffic}</code>{suffix}"
    )


def render_inbound_detail(inbound: InboundSummary) -> str:
    return (
        f"🔌 <b>{esc(inbound.remark)}</b>\n"
        f"Протокол: {esc(inbound.protocol)} · порт {inbound.port}\n"
        f"Состояние: {'включён' if inbound.enabled else 'выключен'}\n"
        f"Клиентов: {inbound.clients}\n"
        f"Отправлено: {fmt_bytes(inbound.up_bytes)}\n"
        f"Принято: {fmt_bytes(inbound.down_bytes)}\n"
        f"Всего: {fmt_bytes(inbound.used_bytes)}\n"
        f"Лимит: {fmt_bytes(inbound.total_bytes) if inbound.total_bytes else '—'}"
    )


def render_finding(finding: Finding) -> str:
    text = f"{severity_icon(finding.severity)} <b>{esc(finding.title)}</b>"
    if finding.detail:
        text += f"\n{esc(finding.detail)}"
    if finding.resolve:
        text += f"\n→ {esc(finding.resolve)}"
    return text


def _cell(value: object, fmt: str = "{}") -> str:
    """Число из базы может оказаться NULL или мусором: не роняем весь отчёт."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return fmt.format(number)


def _stamp(value: object, fmt: str) -> str:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return "—"
    return moment.strftime(fmt)


def render_history(rows: list[sqlite3.Row], *, title: str) -> str:
    if not rows:
        return f"{title}\nПока нет накопленных измерений."
    lines = [f"📈 <b>{esc(title)}</b>", "почасовое среднее (UTC) · CPU / RAM / диск"]
    for row in rows:
        stamp = _stamp(row["bucket"], "%d.%m %H:00")
        lines.append(
            f"{stamp} — {_cell(row['cpu'], '{:.0f}')}% / "
            f"{_cell(row['mem'], '{:.0f}')}% / {_cell(row['disk'], '{:.0f}')}%"
        )
    return "\n".join(lines)


def render_audit(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "Журнал действий пуст."
    lines = ["🧾 <b>Журнал действий</b>"]
    for row in rows:
        stamp = _stamp(row["ts"], "%d.%m %H:%M:%S")
        mark = "✅" if row["ok"] else "❌"
        raw_id = row["telegram_id"]
        who = f"…{str(raw_id)[-3:]}" if raw_id else "—"
        lines.append(f"{mark} {stamp} · {esc(row['action'])} · id {who}")
    return "\n".join(lines)


def render_update(info: UpdateInfo) -> str:
    """Экран проверки обновлений: версия, commit и решение."""
    lines = [
        "🔄 <b>Обновления StackSentry</b>",
        "",
        f"Установлено: <b>{esc(info.current_version)}</b> · "
        f"commit <code>{esc(info.current_short)}</code>",
    ]
    if info.error:
        lines += [
            f"⚠️ Проверка не удалась: {esc(info.error)}",
            "",
            "Проверьте доступ к api.github.com или повторите позже.",
        ]
        return "\n".join(lines)
    lines.append(
        f"В main: commit <code>{esc(info.remote_short)}</code>"
        + (f" · {esc(info.remote_date)}" if info.remote_date else "")
    )
    if info.remote_version:
        lines.append(f"Версия в main: {esc(info.remote_version)}")
    if info.remote_message:
        lines.append(f"Последнее изменение: {esc(info.remote_message)}")
    lines.append("")
    if info.update_available is True:
        lines += [
            "🟢 <b>Доступно обновление.</b>",
            "Нажмите «⬆️ Обновить StackSentry» — потребуются подтверждение и "
            "TOTP. Конфиг и состояние сохранятся.",
            "Вручную: <code>sudo stacksentry-menu</code> → пункт 24.",
        ]
    elif info.update_available is False:
        lines.append("✅ Обновлений нет — установлена актуальная версия main.")
    else:
        lines += [
            "⚪ Не удалось сравнить commit'ы: у этой установки нет метаданных.",
            "Запустите установщик один раз — он запишет commit, и проверка станет "
            "точной.",
        ]
    return "\n".join(lines)


def render_security(settings: Settings, until: float) -> str:
    if not settings.require_2fa:
        return (
            "🔐 <b>Безопасность</b>\n\n"
            "2FA выключена. Mini App работает без кода, опасные действия — "
            "только с подтверждением.\n\n"
            "Включить: <code>sudo stacksentry-menu</code> или повторный "
            "установщик с <code>--2fa</code>. После включения код понадобится "
            "для входа в Mini App и опасных действий."
        )
    if until > time.time():
        status = (
            "🔓 Разблокировано до <b>"
            + time.strftime("%H:%M", time.localtime(until))
            + "</b>"
        )
    else:
        status = "🔒 Заблокировано — нужен код TOTP"
    minutes = max(1, settings.elevation_ttl // 60)
    return (
        "🔐 <b>Безопасность</b>\n\n"
        f"2FA: <b>включена</b>\n{status}\n\n"
        f"Разблокировка действует {minutes} мин. Для обновлений ОС и "
        "перезагрузки нужен свежий код (не старше 2 минут).\n\n"
        "Mini App требует код TOTP при входе. В боте просмотр состояния, "
        "журналов и подписок доступен без кода."
    )


def render_2fa_prompt(title: str, lock_left: float = 0.0) -> str:
    lines = [
        "🔐 <b>Нужен код 2FA</b>",
        f"Действие: {title}",
        "",
    ]
    if lock_left > 0:
        lines.append(f"⏳ Слишком много попыток. Подождите {lock_left:.0f} с.")
    else:
        lines.append(
            "Отправьте 6 цифр из приложения-аутентификатора. Сообщение с кодом я удалю."
        )
    return "\n".join(lines)


def render_command_help(allowlist: tuple[str, ...]) -> str:
    """Список безопасных команд с описаниями, сгруппированный по темам."""
    if not allowlist:
        return (
            "💻 <b>Команда на сервер</b>\n\n"
            "Список команд пуст — выполнение отключено "
            "(<code>COMMAND_ALLOWLIST</code>)."
        )
    grouped: dict[str, list[str]] = {group: [] for group in _COMMAND_GROUPS}
    for command in allowlist:
        group, description = COMMAND_HELP.get(command, ("Прочее", "без описания"))
        grouped.setdefault(group, []).append(
            f"• <code>/run {esc(command)}</code> — {esc(description)}"
        )
    lines = [
        "💻 <b>Команда на сервер</b>",
        "Пришлите команду сообщением в любом разделе или "
        "<code>/run &lt;команда&gt;</code>. Всё только читает, shell недоступен.",
        "У логов (os_logs, xui_logs, kernel_logs, dmesg) под выводом есть "
        "кнопки «Ещё 200 строк» и «Только ошибки».",
    ]
    for group in _COMMAND_GROUPS:
        items = grouped.get(group) or []
        if not items:
            continue
        lines += ["", f"<b>{group}</b>"]
        lines += items
    return "\n".join(lines)


def render_help(
    is_admin: bool,
    *,
    settings: Settings | None = None,
    package_manager: str | None = None,
) -> str:
    """Полная инструкция простыми словами: разделы, действия, настройки."""
    panel_name = panel_title(settings.panel_type) if settings is not None else "3X-UI"
    pm = (
        package_manager
        if package_manager is not None
        else ("apt" if settings is None else "")
    )
    pm_title = PACKAGE_MANAGER_TITLES.get(pm, "пакетный менеджер")
    pm_commands = {
        "apt": ("apt update", "apt full-upgrade"),
        "dnf": ("dnf makecache", "dnf upgrade"),
        "yum": ("yum makecache", "yum update"),
        "zypper": ("zypper refresh", "zypper update"),
        "pacman": ("pacman -Sy", "pacman -Syu"),
        "apk": ("apk update", "apk upgrade"),
        "xbps": ("xbps-install -S", "xbps-install -Su"),
        "emerge": ("emerge --sync", "emerge -uDN @world"),
        "eopkg": ("eopkg update-repo", "eopkg upgrade"),
        "pkg": ("pkg update", "pkg upgrade"),
    }
    update_cmd, upgrade_cmd = pm_commands.get(
        pm, ("команда обновления индексов", "команда обновления системы")
    )
    restart_ok = settings is None or (
        settings.panel_enabled and settings.panel_type in PANEL_RESTART
    )
    lines_out = [
        "🛰 <b>StackSentry</b>",
        "Следит за сервером, панелями и VPN и пишет, когда что-то сломалось. "
        "Управление — кнопками.",
        "",
        "🚀 <b>С чего начать</b>",
        "• <code>/status</code> — сводка: вердикт, метрики, службы, панель.",
        "• <code>/help</code> — эта инструкция; кнопка «❓ Помощь» делает то же.",
        "• «🏠 В меню» есть на каждом экране.",
        "• Команды: <code>/status</code>, <code>/traffic</code>, <code>/subs</code>, "
        "<code>/stack</code>, <code>/history</code>, <code>/logs</code>, "
        "<code>/audit</code>, <code>/maint</code>, <code>/run</code>, "
        "<code>/version</code>, <code>/updatecheck</code>.",
        "",
        "📊 <b>Разделы</b>",
        "🖥 <b>Сервер</b> — CPU, RAM, swap, диск, сеть, load average.",
        f"⚙️ <b>Сервисы</b> — службы (включая {esc(panel_name)}); «Рестарт» и «Журнал».",
        f"🌐 <b>Панель</b> — доступность {esc(panel_name)} и API, версия, обновления.",
        "📊 <b>Трафик</b> — inbound'ы/клиенты или пользователи панели.",
        "🪪 <b>Подписки</b> — сроки, остаток трафика, истёкшие и исчерпанные.",
        "🧩 <b>Стек сервера</b> — что найдено: панели, VPN, прокси, службы.",
        "📈 <b>История</b> — средние CPU/RAM/диска за сутки.",
        "📜 <b>Журнал</b> — вывод службы, «только ошибки», ещё 200 строк.",
        "🗂 <b>Аудит</b> — кто что делал и какие попытки отклонены.",
        "📱 <b>Mini App</b> — Обзор, Службы, Панель, Трафик, Подписки, История, "
        "Терминал и Помощь.",
        "",
        "🌐 <b>Панели</b>",
        "3X-UI, Marzban, Marzneshin, PasarGuard, Remnawave, S-UI, Hiddify, Outline. "
        "Доступ — токен, логин/пароль или секретный путь.",
        "Тип и ключи: <code>sudo stacksentry-menu</code> (пункты 8–12).",
        "Рестарт ядра: 3X-UI, Marzban, Marzneshin, PasarGuard, S-UI; у остальных — "
        "через «Сервисы».",
        "",
        "🖥 <b>ОС и пакеты</b>",
        "systemd, OpenRC, SysV; apt, dnf, yum, zypper, pacman, apk, xbps, emerge, "
        "eopkg, pkg.",
        f"На этом сервере: <b>{esc(pm_title)}</b>.",
        "",
        "🛠 <b>Обслуживание</b> (только администратор)",
        f"• <b>Обновить список пакетов</b> — <code>{esc(update_cmd)}</code>.",
        f"• <b>Обновить систему</b> — <code>{esc(upgrade_cmd)}</code>.",
        "• Phased и <code>do-release-upgrade</code> — только APT/Ubuntu.",
        "• Рестарт ядра панели — "
        + (
            "через API поддерживается."
            if restart_ok
            else "для этой панели недоступен; используйте «Сервисы»."
        ),
        "• Перезагрузка — с подтверждением. Всё идёт в фоне, отчёт придёт сам; "
        "после нового ядра бот напомнит перезагрузиться.",
        "• Обновления StackSentry: «❓ Помощь» → «🔄 Проверить обновления» "
        "или раздел «Помощь» в Mini App; при наличии — кнопка «⬆️ Обновить» "
        "(подтверждение + свежий TOTP).",
        "Примеры: <code>/run os_logs</code>, <code>/run failed_units</code>, "
        "<code>/run sockets</code>.",
        "",
        "💻 <b>Команда на сервер</b>",
        "Пришлите команду или <code>/run df -h</code>: только безопасные команды из "
        "<code>COMMAND_ALLOWLIST</code>, без shell. Список: <code>/run</code>.",
        "",
        "📈 <b>Что значат цифры</b>",
        "CPU, RAM, Диск — проценты: 🟢 норма, 🟡 близко к пределу, 🔴 плохо. Load — "
        "очередь задач; больше числа ядер — сервер не успевает. «Время сервера» — "
        "пояс самого сервера, не телефона.",
        "",
        "🔐 <b>Безопасность</b>",
        "• Команды и кнопки — только в личном чате администратора.",
        "• 2FA (TOTP) — опционально. Если включена при установке: вход в Mini App "
        "и опасные действия в боте требуют код; при генерации показываются QR и "
        "секрет. Если выключена — TOTP не запрашивается нигде. Настроить или "
        "посмотреть QR: <code>sudo stacksentry-menu totp</code>.",
        "• Терминал (если включён) — отдельный TOTP на каждую сессию.",
        "• Подтверждение, одноразовый токен, аудит, маскирование секретов.",
        "• Отдельный пользователь, привилегии — через узкий sudoers-список.",
        "",
        "⚙️ <b>Настройки</b> — <code>sudo stacksentry-menu</code>: тип и адрес "
        "панели, токен или логин/пароль, webBasePath, пороги, службы, логи, "
        "рестарт, обновление, удаление.",
        "Пункт «Что найдено на сервере» покажет ОС, панели и VPN-стек.",
        "",
        "🩺 <b>Если что-то не работает</b>",
        "1. Прочитайте сообщение бота — там причина.",
        "2. <code>/services</code> → «Рестарт» → «Журнал».",
        "3. <code>/stack</code> — что бот реально нашёл.",
        "4. «Панель» — API, <code>PANEL_TYPE</code>, адрес и токен/логин. Для 3X-UI "
        "нужен scope <code>node-sync</code>/<code>admin</code>, а "
        "<code>PANEL_BASE_PATH</code> — «URL панели», не <code>/panel</code>.",
        "5. Журнал: <code>journalctl -u stacksentry-bot -n 50</code>.",
        "",
        "📚 <b>Документация</b> — github.com/Hijackery/stacksentry/tree/main/docs",
    ]
    if not is_admin:
        return (
            "\n".join(lines_out[:12])
            + "\n\n⛔ Управление доступно только администратору."
        )
    return "\n".join(lines_out)
