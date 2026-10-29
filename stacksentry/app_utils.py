"""Вспомогательные функции Telegram-бота."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final, TypeVar
from urllib.parse import urlsplit

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from . import ui
from .alerts import Finding, Rule
from .models import Severity
from .settings import Settings

logger = logging.getLogger(__name__)


def _user_id(update: Update) -> int | None:
    """ID автора апдейта или None, если его нет."""
    return update.effective_user.id if update.effective_user else None


#: Дольше этого flood control не ждём: иначе обработчик «зависает» на минуты
#: и Telegram присылает те же апдейты повторно.
MAX_FLOOD_WAIT: Final = 30.0
FLOOD_ATTEMPTS: Final = 3


_T = TypeVar("_T")


_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)([^>]*?)(/?)>")

#: Шаблон токена бота внутри URL: api.telegram.org/bot123456:AA.../getMe
_BOT_TOKEN_IN_URL = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")


class _ScrubbingFormatter(logging.Formatter):
    """Форматирует запись и вырезает секреты — в том числе из трейсбеков.

    Фильтры на уровне логгера не спасают от чужих библиотек и от текста
    исключений, поэтому чистим уже готовую строку: и токен бота в URL, и точные
    значения секретов из конфигурации.
    """

    def __init__(self, fmt: str, secrets: tuple[str, ...]) -> None:
        super().__init__(fmt)
        #: Короткие значения не вырезаем, чтобы не портить обычный текст.
        self._secrets = tuple(secret for secret in secrets if len(secret) >= 8)

    def _scrub(self, text: str) -> str:
        text = _BOT_TOKEN_IN_URL.sub("bot<redacted>", text)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def format(self, record: logging.LogRecord) -> str:
        return self._scrub(super().format(record))


class _JsonScrubbingFormatter(_ScrubbingFormatter):
    """JSON-лог для journald/agregator: одна строка на запись, без секретов."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return self._scrub(json.dumps(payload, ensure_ascii=False))


def _safe_endpoint(url: str) -> str:
    """Адрес для логов: только схема, хост и порт — без пути и userinfo.

    У Outline секретный ``SB_API_PREFIX`` живёт именно в пути ``PANEL_URL``,
    поэтому логировать полный адрес нельзя.
    """
    if not url:
        return "—"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "—"
    if not parts.scheme or not parts.hostname:
        return "—"
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _parse_apt_status(stdout: str) -> tuple[dict[str, str], str]:
    """Разбирает `stacksentry-ctl apt_status`: шапку STATE/RESULT/EXIT/LOAD и лог."""
    fields: dict[str, str] = {}
    tail: list[str] = []
    header = True
    for line in stdout.splitlines():
        if header:
            key, sep, value = line.partition("=")
            if sep and key in {
                "STATE",
                "RESULT",
                "EXIT",
                "LOAD",
                "PACKAGES",
                "PACKAGE_LIST",
            }:
                fields[key] = value.strip()
                continue
            header = False
        tail.append(line)
    return fields, "\n".join(tail).strip()


def _message_of(query: CallbackQuery) -> Message:
    message = query.message
    if isinstance(message, Message):
        return message
    raise RuntimeError("Кнопка не привязана к обычному сообщению")


def _chat_id(query: CallbackQuery) -> int:
    try:
        return _message_of(query).chat_id
    except RuntimeError:
        return 0


async def _clear_markup(query: CallbackQuery) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError as exc:
        logger.debug("Не удалось убрать кнопки: %s", exc)


async def _safe_edit(
    status: Message, text: str, *, markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await _send_with_retry(
            status.edit_text,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except TelegramError as exc:
        logger.warning("Не удалось обновить статус: %s", exc)


def _alert_actions(
    findings: list[Finding], settings: Settings
) -> list[InlineKeyboardButton]:
    """Кнопки под алертом: починить сервис, о котором идёт речь.

    Сигнал без действия заставляет ночью искать раздел руками, поэтому для
    находки о сервисе сразу даём рестарт и журнал.
    """
    buttons: list[InlineKeyboardButton] = []
    for finding in findings:
        if finding.rule is not Rule.service or ":" not in finding.state_key:
            continue
        unit = finding.state_key.split(":", 1)[1]
        if unit not in settings.services:
            continue
        buttons.append(
            InlineKeyboardButton(
                f"♻️ Рестарт {unit}", callback_data=f"ask:restart_service:{unit}"
            )
        )
        buttons.append(
            InlineKeyboardButton(f"📜 Журнал {unit}", callback_data=f"logs:{unit}")
        )
        break
    return buttons


def _needs_reboot() -> bool:
    return os.path.exists("/var/run/reboot-required")


async def _send_with_retry(
    func: Callable[..., Awaitable[_T]],
    **kwargs: object,
) -> _T | None:
    """Отправка с уважением к flood control Telegram.

    Без этого `RetryAfter` просто логировался, и сообщение (например, результат
    рестарта сервиса) бесследно терялось. Здесь ждём указанную паузу и пробуем
    ещё раз; если Telegram просит слишком долго — сдаёмся с записью в лог.
    """
    for attempt in range(FLOOD_ATTEMPTS):
        try:
            return await func(**kwargs)
        except RetryAfter as exc:
            raw_delay = exc.retry_after
            delay = (
                raw_delay.total_seconds()
                if isinstance(raw_delay, timedelta)
                else float(raw_delay)
            ) + 0.5
            if attempt == FLOOD_ATTEMPTS - 1 or delay > MAX_FLOOD_WAIT:
                logger.warning("Flood control %.0f c: сообщение не отправлено", delay)
                raise
            logger.warning("Flood control: пауза %.0f c, повтор отправки", delay)
            await asyncio.sleep(delay)
    return None


def _open_tags(text: str) -> list[tuple[str, str]]:
    """Стек открытых HTML-тегов: (имя, исходное открытие)."""
    stack: list[tuple[str, str]] = []
    for match in _TAG_RE.finditer(text):
        closing, name, _attrs, self_closing = match.groups()
        if closing:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    del stack[index:]
                    break
        elif not self_closing:
            stack.append((name, match.group(0)))
    return stack


def _balanced_prefix(text: str, limit: int) -> tuple[str, str]:
    """Делит текст на (голова, хвост), не разрывая теги/сущности.

    Голова закрывает открытые теги, хвост — заново их открывает, поэтому
    каждый кусок остаётся валидным HTML для Telegram.
    """
    cut = min(limit, len(text))
    for _ in range(64):
        if cut <= 0:
            break
        head = text[:cut]
        # Не режем внутри тега.
        if head.rfind("<") > head.rfind(">"):
            cut = head.rfind("<")
            continue
        # Не режем внутри сущности (&amp; и т.п.).
        amp, semi = head.rfind("&"), head.rfind(";")
        if amp > semi and " " not in head[amp:] and "<" not in head[amp:]:
            cut = amp
            continue
        stack = _open_tags(head)
        closing = "".join(f"</{name}>" for name, _ in reversed(stack))
        if len(head) + len(closing) <= limit:
            reopening = "".join(opening for _, opening in stack)
            return head + closing, reopening + text[cut:]
        cut -= len(head) + len(closing) - limit
    cut = max(1, min(limit, len(text)))
    return text[:cut], text[cut:]


def _split(text: str, limit: int = 3500) -> list[str]:
    """Режет длинный HTML на части, не разрывая теги и сущности.

    Прямое деление по символам рвёт разметку Telegram («can't parse entities»),
    поэтому режем по переводам строк, а длинную строку — с закрытием и
    повторным открытием тегов на границе.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        newline = rest.rfind("\n", 0, limit)
        cut = newline + 1 if newline >= limit // 2 else limit
        piece, rest = _balanced_prefix(rest, cut)
        if not piece:
            piece, rest = rest[:limit], rest[limit:]
        chunks.append(piece)
    if rest:
        chunks.append(rest)
    return chunks


def _split_alerts(findings: list[Finding]) -> list[str]:
    order = {
        Severity.critical: 0,
        Severity.warning: 1,
        Severity.unknown: 2,
        Severity.ok: 3,
    }
    ordered = sorted(findings, key=lambda item: order[item.severity])
    header = (
        f"🔔 <b>Требует внимания: {len(findings)}</b> · {time.strftime('%H:%M:%S')}"
    )
    body = "\n\n".join(ui.render_finding(finding) for finding in ordered)
    return _split(header + "\n\n" + body)
