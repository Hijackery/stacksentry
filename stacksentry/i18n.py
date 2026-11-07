"""Мини-локализация: язык текущего апдейта через ContextVar."""

from __future__ import annotations

from contextvars import ContextVar

SUPPORTED = ("ru", "en")

_current: ContextVar[str] = ContextVar("stacksentry_language", default="ru")


def normalize(language: str) -> str:
    value = (language or "").strip().lower()
    return value if value in SUPPORTED else "ru"


def set_language(language: str) -> str:
    value = normalize(language)
    _current.set(value)
    return value


def get_language() -> str:
    return _current.get()


def tr(ru: str, en: str) -> str:
    return en if get_language() == "en" else ru
