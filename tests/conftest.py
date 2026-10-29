"""Общие фикстуры: настройки и хранилище во временном каталоге."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from stacksentry.settings import Settings
from stacksentry.storage import Storage


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Собирает валидный Settings с возможностью переопределить поля."""

    def factory(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "telegram_token": "123456789:AAconftest",
            "telegram_api_url": "https://api.telegram.org",
            "admin_ids": (42,),
            "alert_chat_id": None,
            "state_path": tmp_path / "state.db",
            "update_spool_dir": tmp_path / "update",
            "log_path": tmp_path / "logs" / "bot.log",
            "log_level": "INFO",
            "panel_url": "https://panel.example.com",
            "panel_api_token": "secret-token",
            "panel_verify_tls": True,
            "panel_ca_bundle": "",
            "panel_timeout": 5.0,
            "panel_base_path": "",
            "services": ("x-ui",),
            "sudo_wrapper": "/usr/local/bin/stacksentry-ctl",
            "command_allowlist": ("free -h",),
            "check_interval": 60,
            "action_cooldown": 0,
            "confirm_ttl": 120,
            "command_timeout": 60,
            "cpu_warn": 80.0,
            "cpu_crit": 95.0,
            "mem_warn": 85.0,
            "mem_crit": 95.0,
            "disk_warn": 85.0,
            "disk_crit": 93.0,
            "alert_cooldown": 1800,
            "alert_hysteresis": 5.0,
            "expiring_soon_days": 3,
            "disk_path": "/",
        }
        base.update(overrides)
        return Settings(**base)

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "state.db")
