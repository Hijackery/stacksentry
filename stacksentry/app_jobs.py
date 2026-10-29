"""Фоновые задачи Telegram-бота StackSentry."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import timedelta

from telegram import Message
from telegram.ext import ContextTypes

from . import ui
from .actions import ACTION_RELEASE_UPGRADE, APT_KINDS, ActionError
from .alerts import Finding, Rule
from .app_base import BotBase
from .app_constants import (
    ACTION_APT_UPDATE,
    ACTION_REBOOT,
    ACTION_RUNNING,
    ACTION_TITLES,
    ACTION_UPGRADE,
    ACTION_UPGRADE_PHASED,
    APT_WATCH_ERROR_LIMIT,
    APT_WATCH_MAX,
    APT_WATCH_STEP,
    QUEUED_ACTIONS,
    REBOOT_WATCH_ATTEMPTS,
    REBOOT_WATCH_GRACE,
    REBOOT_WATCH_STEP,
    SAMPLE_RETENTION_DAYS,
    UPDATE_NOTIFIED_KEY,
)
from .app_utils import (
    _alert_actions,
    _needs_reboot,
    _parse_apt_status,
    _safe_edit,
    _split_alerts,
)
from .models import Severity
from .updates import check_for_update

logger = logging.getLogger(__name__)


class JobsMixin(BotBase):
    """Плановый сбор, apt-вотчер и отчёты о перезагрузке."""

    async def _verify_after_reboot(self) -> None:
        """Если перед ребутом нажали «Перезагрузить», подтверждаем возврат.

        Сама перезагрузка убивает процесс бота, поэтому проверять сервер
        приходится уже после старта: так сообщение о возврате точно дойдёт.
        """
        job_queue = self.app.job_queue
        if job_queue is None:  # pragma: no cover - JobQueue проверяется при сборке
            return
        rows = await asyncio.to_thread(self.storage.recent_audit, 5)
        reboot = next(
            (row for row in rows if row["action"] == ACTION_REBOOT and row["ok"]),
            None,
        )
        if reboot is None or time.time() - reboot["ts"] > REBOOT_WATCH_GRACE:
            return
        job_queue.run_once(
            self._job_reboot_watch,
            timedelta(seconds=REBOOT_WATCH_STEP),
            name="reboot-watch-after-start",
            data={
                "chat_id": int(reboot["telegram_id"] or 0),
                "attempt": 0,
                "verify": True,
            },
        )

    def _schedule_long_action(
        self,
        action: str,
        chat_id: int,
        user_id: int,
        *,
        message_id: int = 0,
    ) -> None:
        job_queue = self.app.job_queue
        if job_queue is None:  # pragma: no cover - JobQueue проверяется при сборке
            return
        job_queue.run_once(
            self._job_long_action,
            timedelta(seconds=1),
            name=f"long-{action}",
            data={
                "action": action,
                "chat_id": chat_id,
                "user_id": user_id,
                "message_id": message_id,
            },
        )

    async def _job_long_action(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = dict(getattr(context.job, "data", None) or {})
        action = str(data.get("action", ""))
        chat_id = int(data.get("chat_id", 0) or 0)
        user_id = int(data.get("user_id", 0) or 0)
        message_id = int(data.get("message_id", 0) or 0)

        try:
            if action == ACTION_APT_UPDATE:
                result = await self.actions.apt_update()
            elif action == ACTION_UPGRADE:
                result = await self.actions.apt_upgrade()
            elif action == ACTION_UPGRADE_PHASED:
                result = await self.actions.apt_upgrade_phased()
            elif action == ACTION_RELEASE_UPGRADE:
                result = await self.actions.release_upgrade()
            else:
                result = await self.actions.dist_upgrade()
        except Exception as exc:  # noqa: BLE001 — фон не должен молча «зависать»
            logger.error("Фоновая задача %s упала: %s", action, exc, exc_info=True)
            await self._replace_or_notify(
                chat_id,
                message_id,
                f"❌ {ui.esc(str(exc) or exc.__class__.__name__)}",
                markup=ui.result_menu(),
            )
            await asyncio.to_thread(
                self.storage.audit,
                action,
                telegram_id=user_id,
                ok=False,
                result=str(exc) or exc.__class__.__name__,
            )
            return

        # Задача в systemd поставлена. Дальше следим за юнитом сами и пришлём
        # отчёт в чат — пользователю не нужно ничего вводить.
        if action in QUEUED_ACTIONS and result.ok:
            kind = APT_KINDS[action]
            job_id = await asyncio.to_thread(
                self.storage.add_pending_job,
                action,
                kind,
                chat_id,
                user_id,
                message_id,
            )
            self._schedule_apt_watch(
                job_id,
                action,
                chat_id,
                user_id,
                created_at=time.time(),
                message_id=message_id,
            )
            title = ACTION_RUNNING.get(action, action)
            await self._replace_or_notify(
                chat_id,
                message_id,
                f"⏳ <b>{ui.esc(title)} запущено</b>\n"
                "Задача выполняется в фоне. Пришлю отчёт сюда, когда закончится — "
                "ничего вводить не нужно.",
                markup=ui.result_menu(),
            )
            return

        icon = "✅" if result.ok else "⚠️"
        text = (
            f"{icon} <b>{ui.esc(ACTION_TITLES.get(action, result.command))}</b>\n"
            f"код возврата {result.returncode},"
            f" длительность {result.duration:.0f} с\n\n"
            f"<pre>{ui.esc(result.summary(2500))}</pre>"
        )
        if _needs_reboot():
            text += (
                "\n\n🔁 <b>Требуется перезагрузка</b> для применения ядра/пакетов. "
                "Кнопка «Перезагрузить» в разделе Обслуживание."
            )
        delivered = await self._notify(chat_id, text, markup=ui.result_menu())
        await asyncio.to_thread(
            self.storage.audit,
            action,
            telegram_id=user_id,
            params={"background": True},
            ok=result.ok,
            result=result.summary(500),
        )
        self.invalidate()
        if not delivered:
            logger.warning("Отчёт по %s не доставлен", action)

    def _schedule_apt_watch(
        self,
        job_id: int,
        action: str,
        chat_id: int,
        user_id: int,
        *,
        created_at: float,
        message_id: int = 0,
        attempts: int = 0,
        errors: int = 0,
    ) -> None:
        job_queue = self.app.job_queue
        if job_queue is None:  # pragma: no cover
            return
        job_queue.run_once(
            self._job_watch_apt,
            timedelta(seconds=APT_WATCH_STEP),
            name=f"apt-watch-{job_id}",
            data={
                "job_id": job_id,
                "action": action,
                "chat_id": chat_id,
                "user_id": user_id,
                "created_at": created_at,
                "message_id": message_id,
                "attempts": attempts,
                "errors": errors,
            },
        )

    async def _resume_apt_watches(self) -> None:
        rows = await asyncio.to_thread(self.storage.list_pending_jobs)
        for row in rows:
            created_at = float(row["created_at"])
            if time.time() - created_at > APT_WATCH_MAX:
                await asyncio.to_thread(self.storage.remove_pending_job, int(row["id"]))
                continue
            self._schedule_apt_watch(
                int(row["id"]),
                str(row["action"]),
                int(row["chat_id"]),
                int(row["user_id"]),
                created_at=created_at,
                message_id=int(dict(row).get("message_id") or 0),
            )

    async def _job_watch_apt(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = dict(getattr(context.job, "data", None) or {})
        job_id = int(data.get("job_id", 0) or 0)
        action = str(data.get("action", ""))
        chat_id = int(data.get("chat_id", 0) or 0)
        user_id = int(data.get("user_id", 0) or 0)
        created_at = float(data.get("created_at", time.time()))
        message_id = int(data.get("message_id", 0) or 0)
        attempts = int(data.get("attempts", 0) or 0)
        errors = int(data.get("errors", 0) or 0)

        def reschedule(*, attempts: int, errors: int) -> None:
            self._schedule_apt_watch(
                job_id,
                action,
                chat_id,
                user_id,
                created_at=created_at,
                message_id=message_id,
                attempts=attempts,
                errors=errors,
            )

        try:
            status = await self.actions.apt_status(action)
            if status.returncode != 0:
                raise RuntimeError(status.stderr.strip() or "apt_status: ошибка")
            fields, tail = _parse_apt_status(status.stdout)
        except Exception as exc:  # noqa: BLE001 — сеть/права/юнит
            errors += 1
            logger.warning("Не удалось узнать статус %s: %s", action, exc)
            if (
                errors < APT_WATCH_ERROR_LIMIT
                and time.time() - created_at < APT_WATCH_MAX
            ):
                reschedule(attempts=attempts, errors=errors)
            else:
                await self._finish_apt_watch(
                    job_id,
                    action,
                    chat_id,
                    user_id,
                    message_id,
                    ok=False,
                    text="⚠️ Не удалось получить отчёт об обновлении. "
                    "Проверьте: <code>sudo stacksentry-menu apt-logs</code>\n"
                    f"<pre>{ui.esc(str(exc))}</pre>",
                )
            return

        state = fields.get("STATE", "unknown")
        if state in {"active", "activating", "reloading", "deactivating"}:
            if time.time() - created_at > APT_WATCH_MAX:
                await self._finish_apt_watch(
                    job_id,
                    action,
                    chat_id,
                    user_id,
                    message_id,
                    ok=False,
                    text="⚠️ Обновление идёт слишком долго (больше 2 часов). "
                    "Проверьте: <code>sudo stacksentry-menu apt-logs</code>",
                )
            else:
                reschedule(attempts=attempts + 1, errors=errors)
            return

        # Юнит ещё не успел появиться (например, только что скомандовали start).
        if (
            state in {"unknown", ""}
            and fields.get("LOAD") == "not-found"
            and attempts < 3
        ):
            reschedule(attempts=attempts + 1, errors=errors)
            return

        ok = fields.get("RESULT") == "success" and fields.get("EXIT") == "0"
        icon = "✅" if ok else "⚠️"
        packages_block = ""
        if action == ACTION_APT_UPDATE:
            count = fields.get("PACKAGES", "").strip()
            package_list = fields.get("PACKAGE_LIST", "").strip()
            if count == "0":
                packages_block = "📦 <b>Обновлений нет.</b>\n\n"
            elif count:
                shown = ui.esc(package_list[:1500])
                suffix = "…" if len(package_list) > 1500 else ""
                packages_block = (
                    f"📦 <b>К обновлению ({ui.esc(count)}):</b>\n"
                    f"<pre>{shown}{suffix}</pre>\n\n"
                )
        # Список пакетов важнее «шума» systemd: при его наличии оставляем
        # меньше журнала, чтобы сообщение целиком влезло в Telegram.
        body = tail[-(1200 if packages_block else 2500) :] if tail else "(вывод пуст)"
        text = (
            f"{icon} <b>{ui.esc(ACTION_TITLES.get(action, action))}</b>\n"
            f"код возврата {ui.esc(fields.get('EXIT', 'unknown'))}\n\n"
            f"{packages_block}<pre>{ui.esc(body)}</pre>"
        )
        if ok and _needs_reboot():
            text += (
                "\n\n🔁 <b>Требуется перезагрузка</b> для применения ядра/пакетов. "
                "Кнопка «Перезагрузить» в разделе Обслуживание."
            )
        await self._finish_apt_watch(
            job_id, action, chat_id, user_id, message_id, ok=ok, text=text
        )

    async def _finish_apt_watch(
        self,
        job_id: int,
        action: str,
        chat_id: int,
        user_id: int,
        message_id: int,
        *,
        ok: bool,
        text: str,
    ) -> None:
        await asyncio.to_thread(self.storage.remove_pending_job, job_id)
        delivered = await self._replace_or_notify(
            chat_id, message_id, text, markup=ui.result_menu()
        )
        await asyncio.to_thread(
            self.storage.audit,
            action,
            telegram_id=user_id,
            params={"background": True},
            ok=ok,
            result="watcher",
        )
        self.invalidate()
        if not delivered:
            logger.warning("Отчёт по %s не доставлен", action)

    async def _do_reboot(self, status: Message, user_id: int) -> None:
        chat_id = status.chat_id
        await _safe_edit(status, "🔁 Перезагружаю сервер…")
        try:
            result = await self.actions.reboot()
        except ActionError as exc:
            await asyncio.to_thread(
                self.storage.audit,
                ACTION_REBOOT,
                telegram_id=user_id,
                ok=False,
                result=str(exc),
            )
            await _safe_edit(status, f"❌ {ui.esc(str(exc))}", markup=ui.result_menu())
            return

        await asyncio.to_thread(
            self.storage.audit,
            ACTION_REBOOT,
            telegram_id=user_id,
            ok=result.ok,
            result=result.summary(300),
        )
        if not result.ok:
            await _safe_edit(
                status,
                f"⚠️ Reboot не выполнен: {ui.esc(result.summary(300))}",
                markup=ui.result_menu(),
            )
            return

        await _safe_edit(
            status,
            "🔁 Сервер перезагружается. После старта я проверю, что всё поднялось.",
        )
        self._schedule_reboot_watch(chat_id, verify=True)

    def _schedule_reboot_watch(self, chat_id: int, *, verify: bool) -> None:
        job_queue = self.app.job_queue
        if job_queue is None:  # pragma: no cover
            return
        job_queue.run_once(
            self._job_reboot_watch,
            timedelta(seconds=REBOOT_WATCH_STEP),
            name="reboot-watch",
            data={"chat_id": chat_id, "attempt": 0, "verify": verify},
        )

    async def _job_reboot_watch(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Проверяет возврат сервера, не блокируя очередь задач.

        Пока сервер не поднялся, задача перепланирует сама себя: sleep внутри
        JobQueue остановил бы плановые проверки и алерты.
        """
        job_queue = context.job_queue
        if job_queue is None:  # pragma: no cover - очередь есть при run_polling
            return
        data = dict(getattr(context.job, "data", None) or {})
        chat_id = int(data.get("chat_id", 0) or 0)
        attempt = int(data.get("attempt", 0)) + 1
        verify = bool(data.get("verify", False))

        service_down = bool(
            self.settings.services
        ) and not await self.monitor.services.is_active(self.settings.services[0])
        if service_down:
            if attempt < REBOOT_WATCH_ATTEMPTS:
                job_queue.run_once(
                    self._job_reboot_watch,
                    timedelta(seconds=REBOOT_WATCH_STEP),
                    name="reboot-watch",
                    data={"chat_id": chat_id, "attempt": attempt, "verify": verify},
                )
                return
            await self._notify(
                chat_id,
                "⚠️ Сервер не поднялся дольше 10 минут. Проверьте консоль VDS.",
            )
            return

        if not verify:
            return

        snapshot = await self.snapshot(force=True)
        services_ok = "services" not in snapshot.unavailable and all(
            service.active for service in snapshot.services
        )
        panel_ok = snapshot.panel.reachable or snapshot.panel.disabled
        panel_text = (
            "не настроена"
            if snapshot.panel.disabled
            else ("отвечает" if snapshot.panel.reachable else "не отвечает")
        )
        verdict = (
            "✅ Сервер вернулся" if services_ok and panel_ok else "⚠️ Вернулся частично"
        )
        await self._notify(
            chat_id,
            f"{verdict}\n"
            f"Сервисы: {'ок' if services_ok else 'есть проблемы'} · "
            f"панель: {panel_text}",
        )

    async def job_update_check(self, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Один раз уведомляет админов о новом commit в main."""
        try:
            info = await asyncio.to_thread(check_for_update)
        except Exception:  # noqa: BLE001 — фоновая проверка не должна падать
            logger.debug("Проверка обновлений не удалась", exc_info=True)
            return
        if info.error or info.update_available is not True or not info.remote_sha:
            return
        last = await asyncio.to_thread(self.storage.get_meta, UPDATE_NOTIFIED_KEY)
        if last == info.remote_sha:
            return
        delivered = True
        for chat_id in self.settings.admin_ids:
            delivered = (
                await self._notify(
                    chat_id, ui.render_update(info), markup=ui.update_menu(info)
                )
                and delivered
            )
        if delivered:
            await asyncio.to_thread(
                self.storage.set_meta, UPDATE_NOTIFIED_KEY, info.remote_sha
            )

    def _quiet_hours(self) -> bool:
        start_text = self.settings.alert_quiet_start
        end_text = self.settings.alert_quiet_end
        if not start_text or not end_text:
            return False
        try:
            start_h, start_m = (int(part) for part in start_text.split(":"))
            end_h, end_m = (int(part) for part in end_text.split(":"))
        except ValueError:
            return False
        now = time.localtime()
        moment = now.tm_hour * 60 + now.tm_min
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start == end:
            return False
        if start < end:
            return start <= moment < end
        return moment >= start or moment < end

    async def _flush_alert_outbox(self) -> None:
        """Отправляет накопленные алерты и подтверждает их только после успеха."""
        rows = await asyncio.to_thread(self.storage.pending_alerts)
        if not rows:
            return
        quiet = self._quiet_hours()
        routes: dict[str, tuple[tuple[int, int | None], ...]] = {}
        groups: dict[tuple[tuple[int, int | None], ...], list[sqlite3.Row]] = {}
        for row in rows:
            severity = str(row["severity"])
            # Ночью копим предупреждения и отправляем дайджестом утром.
            if quiet and severity != "critical":
                continue
            if severity not in routes:
                routes[severity] = tuple(await self._alert_targets(severity))
            route = routes[severity]
            if not route:
                continue
            groups.setdefault(route, []).append(row)
        if not groups:
            return
        for route, group_rows in groups.items():
            findings: list[Finding] = []
            for row in group_rows:
                try:
                    rule = Rule(str(row["rule"]))
                except ValueError:
                    rule = Rule.panel
                findings.append(
                    Finding(
                        rule,
                        Severity(str(row["severity"])),
                        str(row["title"]),
                        str(row["detail"]),
                        key=str(row["state_key"]),
                        resolve=str(row["resolve"]),
                    )
                )
            chunks = _split_alerts(findings)
            markup = ui.alert_menu(
                _alert_actions(findings, self.settings),
                mute_key=findings[0].state_key,
            )
            delivered = True
            for chat_id, thread_id in route:
                for index, chunk in enumerate(chunks):
                    delivered = (
                        await self._notify(
                            chat_id,
                            chunk,
                            markup=markup if index == 0 else None,
                            thread_id=thread_id,
                        )
                        and delivered
                    )
            if delivered:
                now = time.time()
                keys = {str(row["state_key"]) for row in group_rows}
                for row in group_rows:
                    await asyncio.to_thread(
                        self.storage.mark_alert_sent_row, int(row["id"]), now
                    )
                await asyncio.to_thread(self.engine.mark_sent, list(keys), at=now)
                continue
            retry_at = time.time() + 30
            for row in group_rows:
                await asyncio.to_thread(
                    self.storage.retry_alert,
                    int(row["id"]),
                    "Telegram не принял сообщение",
                    retry_at,
                )
            logger.warning("Алерты не доставлены, outbox повторит отправку")

    async def job_check(self, _context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            snapshot = await self.monitor.snapshot()
        except Exception:  # фоновая задача не должна падать
            logger.exception("Плановый сбор не удался")
            return

        self._snapshot = snapshot
        self._snapshot_at = time.monotonic()

        if snapshot.system is not None:
            try:
                await asyncio.to_thread(self.storage.record_sample, snapshot.system)
                await asyncio.to_thread(
                    self.storage.prune_samples,
                    time.time() - SAMPLE_RETENTION_DAYS * 86400,
                )
            except sqlite3.Error as exc:
                logger.warning("Не удалось записать метрики в базу: %s", exc)

        findings = self.engine.evaluate(snapshot)
        try:
            outbox = await asyncio.to_thread(
                self.engine.update, findings, unavailable=snapshot.unavailable
            )
        except sqlite3.Error as exc:
            logger.warning("Не удалось обновить состояние алертов: %s", exc)
            return
        for finding in outbox:
            await asyncio.to_thread(
                self.storage.enqueue_alert,
                state_key=finding.state_key,
                rule=finding.rule.value,
                severity=finding.severity.value,
                title=finding.title,
                detail=finding.detail,
                resolve=finding.resolve,
            )
        await self._flush_alert_outbox()

    async def job_prune(self, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await asyncio.to_thread(
            self.storage.prune_samples, time.time() - SAMPLE_RETENTION_DAYS * 86400
        )
        await asyncio.to_thread(self.storage.prune_audit, time.time() - 90 * 86400)
        await asyncio.to_thread(
            self.storage.prune_alert_outbox, time.time() - 30 * 86400
        )
