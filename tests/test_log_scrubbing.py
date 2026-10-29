"""Секреты не должны попадать в лог — даже через трейсбеки библиотек.

Проверяем не только наши сообщения, но и чужой текст: форматтер чистит уже
готовую строку, поэтому перехватывает и URL вида api.telegram.org/bot<TOKEN>,
и значения токенов из конфигурации в тексте исключений.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from stacksentry.app import _JsonScrubbingFormatter, _mask_id, _ScrubbingFormatter

BOT_TOKEN = "123456789:AASECRETvalue1234567890"
PANEL_TOKEN = "panel-secret-token-1234"


def _capture(message: str, *, with_traceback: bool = False) -> str:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_ScrubbingFormatter("%(message)s", (BOT_TOKEN, PANEL_TOKEN)))
    logger = logging.getLogger("scrub-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    if with_traceback:
        try:
            raise RuntimeError(f"boom {BOT_TOKEN}")
        except RuntimeError:
            logger.exception(message)
    else:
        logger.warning(message)
    return stream.getvalue()


def test_bot_token_inside_api_url_is_redacted() -> None:
    text = _capture(f"request to https://api.telegram.org/bot{BOT_TOKEN}/getMe failed")

    assert BOT_TOKEN not in text
    assert "bot<redacted>" in text


def test_exact_secret_from_settings_is_redacted() -> None:
    text = _capture(f"panel rejected the request with {PANEL_TOKEN}")

    assert PANEL_TOKEN not in text
    assert "***" in text


def test_traceback_text_is_redacted_too() -> None:
    """Токен в трейсбеке — самая частая утечка мимо фильтров логгеров."""
    text = _capture("плановый сбор не удался", with_traceback=True)

    assert BOT_TOKEN not in text
    assert "bot<redacted>" in text or "***" in text


def test_json_log_is_one_line_and_redacts_secrets() -> None:
    formatter = _JsonScrubbingFormatter("%(message)s", (BOT_TOKEN,))
    record = logging.LogRecord(
        "t",
        logging.WARNING,
        __file__,
        1,
        f"request with {BOT_TOKEN}",
        None,
        None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "t"
    assert BOT_TOKEN not in payload["message"]
    assert "***" in payload["message"]


def test_short_values_are_left_alone() -> None:
    """Короткие значения не вырезаем: иначе пострадает обычный текст."""
    formatter = _ScrubbingFormatter("%(message)s", ("short",))
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "value short here", None, None
    )

    assert formatter.format(record) == "value short here"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(123456789, "…789"), (42, "…"), (None, "—")],
)
def test_chat_ids_are_masked(value: int | None, expected: str) -> None:
    assert _mask_id(value) == expected
