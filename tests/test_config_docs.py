"""Конфигурация не должна расходиться с кодом и документацией.

Ошибки этого класса не видно в тестах функций: ключ просто молча
игнорируется, и пользователь правит `.env` без эффекта.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stacksentry.settings import BASE_DIR, ConfigError, load_settings

ENV_EXAMPLE = BASE_DIR / ".env.example"
SETTINGS_PY = BASE_DIR / "stacksentry" / "settings.py"
README = BASE_DIR / "README.md"
XUI_CTL = BASE_DIR / "install" / "stacksentry-ctl"


def _example_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0])
    return keys


def _codes_keys() -> set[str]:
    source = SETTINGS_PY.read_text(encoding="utf-8")
    return set(re.findall(r'env_\w+\(\s*"([A-Z][A-Z0-9_]*)"', source))


def test_env_example_keys_are_all_read_by_code() -> None:
    unknown = sorted(_example_keys() - _codes_keys())
    assert not unknown, f"ключи из .env.example никто не читает: {unknown}"


def test_code_keys_are_all_in_env_example() -> None:
    missing = sorted(_codes_keys() - _example_keys())
    assert not missing, f"код читает ключи, которых нет в .env.example: {missing}"


def test_readme_documents_every_env_example_key() -> None:
    documented = set(
        re.findall(
            r"^\|\s*`([A-Z][A-Z0-9_]*)`", README.read_text(encoding="utf-8"), re.M
        )
    )
    missing = sorted(_example_keys() - documented)
    assert not missing, f"ключи не описаны в README: {missing}"


def test_version_is_synced_with_readme_and_changelog() -> None:
    from stacksentry import __version__

    readme = README.read_text(encoding="utf-8")
    changelog = (BASE_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"**{__version__}**" in readme
    assert f"## [{__version__}]" in changelog


def test_filled_example_passes_validation(tmp_path: Path) -> None:
    """Пример из репозитория должен быть валиден после заполнения."""
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    values.update(
        TELEGRAM_BOT_TOKEN="123456789:AArealish",
        TELEGRAM_ADMIN_IDS="42",
        PANEL_URL="https://panel.example.com:2053",
        PANEL_API_TOKEN="token",
    )
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8"
    )

    settings = load_settings(path)
    assert settings.admin_ids == (42,)
    assert settings.services == ("x-ui", "fail2ban")
    # Каталог состояния по умолчанию — вне каталога с кодом.
    assert str(settings.state_path) == "/var/lib/stacksentry/state.db"


def test_example_placeholder_token_is_rejected(tmp_path: Path) -> None:
    """Заглушка из `.env.example` обязана падать сразу, а не с 401 от Telegram."""
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenReplaceMe\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com\n"
        "PANEL_API_TOKEN=token\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="@BotFather"):
        load_settings(path)


def test_token_without_bot_id_separator_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=just-a-string\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com\n"
        "PANEL_API_TOKEN=token\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="<bot_id>"):
        load_settings(path)


def test_panel_url_scheme_is_validated(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AArealish\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=panel.example.com\n"
        "PANEL_API_TOKEN=token\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="http"):
        load_settings(path)


def test_command_allowlist_matches_wrapper_whitelist() -> None:
    """Команда из COMMAND_ALLOWLIST обязана быть разрешена обёрткой.

    Иначе бот отвечает «команда не разрешена» на команды, которые сам же
    предлагает в .env.example, и разобраться сложно.
    """
    allowlist_raw = re.search(
        r"^COMMAND_ALLOWLIST=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M
    )
    assert allowlist_raw is not None
    configured = [item.strip() for item in allowlist_raw.group(1).split(",") if item]

    wrapper = set(
        re.findall(
            r'"([a-z0-9][a-z0-9 ._-]*)"\s*[)|]',
            XUI_CTL.read_text(encoding="utf-8"),
        )
    )
    unknown = sorted(command for command in configured if command not in wrapper)
    assert not unknown, (
        f"COMMAND_ALLOWLIST разрешает то, что stacksentry-ctl запрещает: {unknown}"
    )


def test_wrapper_subcommands_match_sudoers() -> None:
    """Каждая пользовательская подкоманда обёртки должна быть в sudoers.

    Иначе бот получает «sudo: command not allowed» вместо ожидаемой ошибки.
    """
    sudoers = (BASE_DIR / "install" / "stacksentry-sudoers").read_text(encoding="utf-8")
    allowed = set(re.findall(r"stacksentry-ctl (\w+)", sudoers))
    subcommands = set(
        re.findall(r"^    (\w+)\)$", XUI_CTL.read_text(encoding="utf-8"), re.M)
    )

    public = {name for name in subcommands if not name.startswith("_")}
    internal = {name for name in subcommands if name.startswith("_")}
    missing = sorted(public - allowed)
    assert not missing, f"подкоманды не разрешены в sudoers: {missing}"

    # Внутренние подкоманды (с подчёркиванием) вызываются только transient-
    # юнитом systemd и через sudo достижимы быть не должны.
    leaked = sorted(internal & allowed)
    assert not leaked, f"внутренние подкоманды попали в sudoers: {leaked}"


def test_every_safe_command_has_a_wrapper_mapping() -> None:
    """Любая команда из COMMAND_ALLOWLIST должна быть явно разрешена обёрткой."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    line = next(
        item for item in text.splitlines() if item.startswith("COMMAND_ALLOWLIST=")
    )
    commands = [
        item.strip() for item in line.split("=", 1)[1].split(",") if item.strip()
    ]
    wrapper = XUI_CTL.read_text(encoding="utf-8")
    missing = []
    for command in commands:
        key = command.replace(" ", "_")
        if f'"{key}"' not in wrapper and f'"{command}"' not in wrapper:
            missing.append(command)
    assert not missing, f"нет ветки в stacksentry-ctl run_command: {missing}"
    assert len(commands) >= 20
