"""Установщик: разбор опций, справка, статическая проверка обоих скриптов.

`install.sh` запускается из интернета под root, поэтому даже разбор опций
стоит на тестах: ошибка здесь означает, что одна команда установки упадёт
у всех сразу.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stacksentry.settings import BASE_DIR

INSTALL_SH = BASE_DIR / "install.sh"
XUI_CTL = BASE_DIR / "install" / "stacksentry-ctl"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_help_lists_install_options_and_exits_zero() -> None:
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    for fragment in (
        "--tarball",
        "--prefix",
        "--yes",
        "--no-start",
        "--menu",
        "--uninstall",
        "--purge",
        "--keep-state",
        "--no-terminal",
        "--no-webapp",
        "--no-root-terminal",
        "TARBALL_URL",
    ):
        assert fragment in result.stdout, fragment


def test_fresh_interactive_install_enables_full_stack() -> None:
    """Обычная установка не должна требовать второй команды для Mini App/терминала."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]' in text
    assert '[ ! -f "$ENV_FILE" ]' in text
    assert "TERMINAL_ALLOW_ROOT=1" in text
    assert "configure_2fa" in text
    # На обновлении уже установленные компоненты не теряются без --no-*.
    assert "stacksentry-terminal.service" in text
    assert "env_get WEBAPP_ENABLED" in text
    # --2fa должен иметь приоритет над старым значением в .env.
    assert "--2fa)          TWOFA_EXPLICIT=1; shift ;;" in text


@pytest.mark.skipif(
    os.geteuid() == 0, reason="под root удаление реально сработает — недопустимо"
)
@pytest.mark.parametrize("flag", ["--uninstall", "--purge"])
def test_uninstall_requires_root(flag: str) -> None:
    """Удаление — операция с root-правами: без них только понятный отказ."""
    result = _run(flag)
    assert result.returncode != 0
    assert "Запускать от root" in result.stderr


def test_unknown_option_fails_before_any_system_change() -> None:
    result = _run("--boom")
    assert result.returncode != 0
    assert "Неизвестная опция" in result.stderr
    # Отказ происходит до шагов установки: ни один системный вызов не звучит.
    assert "Проверяем систему" not in result.stdout


def test_tarball_option_requires_a_url() -> None:
    result = _run("--tarball")
    assert result.returncode != 0
    assert "--tarball требует URL" in result.stderr


def test_install_sh_is_syntactically_valid() -> None:
    subprocess.run(["bash", "-n", str(INSTALL_SH)], check=True, timeout=30)


def test_prompts_read_from_tty_when_stdin_is_a_pipe() -> None:
    """Регрессия: при установке `curl … | sudo bash` stdin занят скриптом.

    Без запасного чтения с /dev/tty вопросы молча пропускались и установка
    шла на значениях по умолчанию — без токенов и адреса панели.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "exec 3</dev/tty" in text
    assert "read -r __answer <&$READ_FD" in text
    assert "[ -t 0 ]" in text


def test_print_source_shows_default_repository() -> None:
    """По умолчанию источником должен быть реальный репозиторий, не заглушка."""
    result = _run("--print-source")
    assert result.returncode == 0, result.stderr
    assert "REPO_URL=https://github.com/Hijackery/stacksentry" in result.stdout
    assert "placeholder=no" in result.stdout


def test_print_source_marks_placeholder_repository() -> None:
    """Страховка: явная заглушка OWNER/REPO должна быть видна сразу."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--print-source"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "REPO_OWNER": "OWNER", "REPO_NAME": "REPO"},
    )
    assert result.returncode == 0, result.stderr
    assert "placeholder=yes" in result.stdout


def test_print_source_resolves_repo_and_ref() -> None:
    """--repo/--ref дают ровно тот адрес, с которого пойдёт установка."""
    result = _run("--repo", "mylogin/stacksentry-bot", "--ref", "dev", "--print-source")
    assert result.returncode == 0, result.stderr
    assert "REPO_URL=https://github.com/mylogin/stacksentry-bot" in result.stdout
    assert (
        "TARBALL_URL=https://codeload.github.com/mylogin/stacksentry-bot"
        "/tar.gz/refs/heads/dev" in result.stdout
    )
    assert "placeholder=no" in result.stdout


def test_print_source_respects_repo_url_env() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--print-source"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "REPO_URL": "https://github.com/someone/bot.git"},
    )
    assert result.returncode == 0, result.stderr
    assert "codeload.github.com/someone/bot/tar.gz" in result.stdout


def test_repo_option_requires_owner_and_name() -> None:
    result = _run("--repo", "no-slash", "--print-source")
    assert result.returncode != 0
    assert "owner/name" in result.stderr


def test_candidates_fall_back_to_main_and_master() -> None:
    """Ветка репозитория может называться иначе — пробуем частые варианты."""
    result = _run("--repo", "me/bot", "--ref", "dev", "--print-source")
    assert result.returncode == 0, result.stderr
    candidates = next(
        line for line in result.stdout.splitlines() if line.startswith("CANDIDATES=")
    )
    assert "/refs/heads/dev" in candidates
    assert "/refs/heads/main" in candidates
    assert "/refs/heads/master" in candidates


def test_github_tarball_download_is_cache_busted() -> None:
    """Регрессия: codeload кэширует архив ветки и отдаёт устаревший снимок.

    Без метки времени установка сразу после обновления репозитория ставила бы
    прошлую версию кода.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert '_fetch_url="${_fetch_url}?t=$(date +%s)"' in text
    assert "*codeload.github.com/*)" in text


def test_installer_supports_archive_checksum() -> None:
    """Установщик исполняется от root: должна быть возможность привязки к версии."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "SOURCE_SHA256" in text
    assert "--sha256" in text
    assert "verify_archive" in text


def _verify_archive_probe(
    tmp_path: Path, expected: str
) -> subprocess.CompletedProcess[str]:
    """Вытаскиваем функцию verify_archive из установщика и запускаем её отдельно."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("verify_archive() {")
    end = script.index("\n}\n", start) + 3
    payload = tmp_path / "src.tar.gz"
    payload.write_bytes(b"payload")
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'fail() { echo "FAIL: $1" >&2; exit 9; }\n'
        'warn() { echo "WARN: $1" >&2; }\n'
        'ok() { echo "OK: $1"; }\n'
        f"{script[start:end]}\n"
        f"SOURCE_SHA256={expected}\n"
        f'verify_archive "{payload}"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, timeout=30, check=False
    )


def test_checksum_accepts_matching_archive(tmp_path: Path) -> None:
    import hashlib

    digest = hashlib.sha256(b"payload").hexdigest()
    result = _verify_archive_probe(tmp_path, digest)
    assert result.returncode == 0, result.stderr
    assert "совпала" in result.stdout


def test_checksum_rejects_tampered_archive(tmp_path: Path) -> None:
    result = _verify_archive_probe(tmp_path, "0" * 64)
    assert result.returncode == 1
    assert "не совпала" in result.stderr


@pytest.mark.skipif(
    shutil.which("shellcheck") is None, reason="shellcheck не установлен"
)
@pytest.mark.parametrize("script", [INSTALL_SH, XUI_CTL], ids=lambda p: p.name)
def test_scripts_pass_shellcheck(script: Path) -> None:
    """shellcheck — в обязательной обходной карте, не только вручную."""
    result = subprocess.run(
        ["shellcheck", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_installer_installs_doctor_command() -> None:
    text = (BASE_DIR / "install.sh").read_text(encoding="utf-8")
    assert "DOCTOR_PATH" in text
    assert "stacksentry-doctor" in text
    script = (BASE_DIR / "install" / "stacksentry-doctor").read_text(encoding="utf-8")
    assert "stacksentry.doctor" in script


def test_installer_keeps_local_copy_for_xui_menu_update() -> None:
    """`stacksentry-menu update` берёт установщик рядом с ботом, а не только из сети."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert '"$SOURCE_DIR/install.sh" "$PREFIX/install.sh"' in text


def test_installer_never_asks_to_dump_env_to_terminal() -> None:
    """Подсказка не должна предлагать вывести .env (в нём токены) на экран."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "grep -v '^#' $ENV_FILE" not in text
    assert "не пересылайте" in text


def test_prefix_exists_before_env_is_written() -> None:
    """Регрессия: мастер пишет .env до копирования кода.

    На чистой машине каталога /opt/stacksentry ещё нет, поэтому установщик
    обязан создать его до первого касания .env — иначе установка падает
    на `touch: cannot touch .../.env: No such file or directory`.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    first_mkdir = text.index('mkdir -p "$PREFIX"')
    # Инвариант — про запись, а не про присваивание переменной: каталог обязан
    # существовать до первого касания файла.
    first_write = min(
        text.index('cp "$SOURCE_DIR/.env.example" "$ENV_FILE"'),
        text.index('touch "$ENV_FILE"'),
    )
    assert first_mkdir < first_write, "каталог установки создаётся после записи .env"


def test_incomplete_env_does_not_block_install() -> None:
    """Регрессия: .env после прерванного запуска не должен требовать правки руками.

    На реальной машине установка падала с «В .env пусто PANEL_API_TOKEN» сразу
    после неудачного прогона — вместо этого мастер обязан дозаполнить значения.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "CONFIG_COMPLETE=0" in text
    assert "дозаполняю через мастер" in text
    assert "for _key in TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_IDS; do" in text
    assert (
        'if [ -n "$(env_get PANEL_URL)" ] || [ -n "$(env_get PANEL_API_TOKEN)" ]'
        in text
    )


def test_url_normalizers_strip_slashes_and_whitespace(tmp_path: Path) -> None:
    """Регрессия: «https://host:5580/» + webBasePath «/panel/» давали двойной слеш.

    На реальной машине это выглядело как «Панель не ответила на
    https://host:5580//» и ломало адрес API.
    """
    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("normalize_url() {")
    end = script.index("\n}\n", script.index("normalize_path() {")) + 3
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"{script[start:end]}\n"
        'printf "1:%s\\n" "$(normalize_url " https://host:5580/// ")"\n'
        'printf "2:%s\\n" "$(normalize_path "/panel/")"\n'
        'printf "3:[%s]\\n" "$(normalize_path "/")"\n'
        'printf "4:%s\\n" "$(normalize_path "https://host:5580/")"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, timeout=30, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "1:https://host:5580" in result.stdout
    assert "2:panel" in result.stdout
    assert "3:[]" in result.stdout, "«/» должен означать «путь не задан»"
    assert "4:https://host:5580" in result.stdout


def test_installer_explains_where_to_get_panel_token() -> None:
    """Пользователь не должен гадать, где взять токен панели."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "Настройки → API Tokens" in text
    assert "x-ui setting -getApiToken" in text
    assert "в v3 эта команда больше не создаёт токены" in text
    assert "node-sync" in text


def test_installer_splits_path_out_of_the_panel_address(tmp_path: Path) -> None:
    """Адрес из браузера (с путём) не должен превращаться в «/panel/panel/api»."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("split_panel_address() {")
    end = script.index("\n}\n", start) + 3
    helpers = script[script.index("normalize_url() {") : end]
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"{helpers}\n"
        'printf "1:[%s]\n" "$(split_panel_address "https://host:5580/panel/")"\n'
        'printf "2:[%s]\n" "$(split_panel_address "https://host:5580")"\n'
        'printf "3:[%s]\n" "$(split_panel_address "http://1.2.3.4:2053")"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, timeout=30, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "1:[https://host:5580 panel]" in result.stdout
    assert "2:[https://host:5580 ]" in result.stdout
    assert "3:[http://1.2.3.4:2053 ]" in result.stdout


def test_installer_explains_optional_alert_chat() -> None:
    """Поле «чат для алертов» необязательное — установщик должен это сказать."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "Enter — админам в личку" in text
    assert "у групп оно отрицательное" in text
    assert "Сервисы можно перечислить через запятую" in text


def test_installer_shows_visudo_reason_and_falls_back() -> None:
    """Раньше причина отказа sudoers скрывалась, а установка обрывалась.

    Теперь установщик печатает строку от visudo и пробует упрощённые правила
    без Defaults — это совместимо с любыми сборками sudo.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "visudo ответил" in text
    assert "Пробую упрощённые правила" in text
    assert "упрощённый вариант" in text


def test_sudoers_sample_has_no_defaults_line() -> None:
    """Никаких Defaults: sudo-rs не знает даже log_output и роняет разбор."""
    text = (BASE_DIR / "install" / "stacksentry-sudoers").read_text(encoding="utf-8")
    defaults = [line for line in text.splitlines() if line.startswith("Defaults")]
    assert defaults == [], defaults


def test_generated_sudoers_has_no_wildcards_or_aliases() -> None:
    """sudo-rs (Ubuntu 26.04+) запрещает «*» в аргументах команд.

    Поэтому установщик перечисляет точные команды: юниты из SERVICES, ключи
    команд из COMMAND_ALLOWLIST (пробел → «_»), без алиасов и без Defaults.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "sudoers_rules() {" in text
    assert "$WRAPPER_PATH $sub $unit" in text
    assert "$WRAPPER_PATH run_command $key" in text
    for forbidden in ("User_Alias", "Cmnd_Alias", "Defaults:"):
        assert forbidden not in text, forbidden

    sample = (BASE_DIR / "install" / "stacksentry-sudoers").read_text(encoding="utf-8")
    assert "*" not in sample
    assert sample.count("NOPASSWD:") == 1


def test_sudoers_template_still_valid_after_stripping_comments(tmp_path: Path) -> None:
    import subprocess

    stripped = tmp_path / "sudoers.stripped"
    source = (BASE_DIR / "install" / "stacksentry-sudoers").read_text(encoding="utf-8")
    stripped.write_text(
        "\n".join(line for line in source.splitlines() if not line.startswith("#"))
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["visudo", "-cf", str(stripped)], capture_output=True, text=True, check=False
    )
    if result.returncode == 127:
        return  # visudo нет — проверка пропускается
    assert result.returncode == 0, result.stdout + result.stderr


def test_token_detection_ignores_scope_names(tmp_path: Path) -> None:
    """Регрессия: в .env попадало «update-all-geofiles» — это scope, а не токен."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("detect_panel_token() {")
    end = script.index("\n}\n", start) + 3

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    fake_xui = shim_dir / "x-ui"
    fake_xui.write_text(
        "#!/bin/sh\ncat <<'OUT'\n"
        "API token generated successfully.\n"
        "Available scopes: update-all-geofiles, node-sync\n"
        "Token name: monitor-bot\n"
        "Token: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6\n"
        "OUT\n",
        encoding="utf-8",
    )
    fake_xui.chmod(0o755)

    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"PYTHON_BIN={shutil.which('python3') or '/usr/bin/python3'}\n"
        f"{script[start:end]}\n"
        "detect_panel_token\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "PATH": f"{shim_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def test_installer_mentions_v3_token_location() -> None:
    """В 3X-UI v3 токен лежит в «Учётной записи», а не в «API Tokens»."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "Учётная запись" in text
    assert "копируйте сразу" in text
    assert "apiTokens/create" in text
    assert "node-sync" in text


def test_uninstall_removes_state_by_default() -> None:
    """«Под ноль» — значит вместе с базой и пользователем (--keep-state отменяет)."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'rm -rf "$STATE_DIR"' in text
    assert "--keep-state" in text
    assert "Остался каталог состояния" not in text
    assert "Бот удалён." in text
    # Отчёт короткий: пошаговых «что и где удалено» больше нет.
    assert "Удалено всё: служба" not in text
    assert "Каталог с кодом удалён" not in text


def test_menu_does_not_reopen_after_uninstall() -> None:
    """После удаления stacksentry-menu завершается: бота и самого меню уже нет."""
    text = (BASE_DIR / "install" / "stacksentry-menu").read_text(encoding="utf-8")
    marker = "Бот и само меню удалены — возвращаться некуда."
    assert marker in text
    assert "exit 0" in text[text.index(marker) :]


def test_installer_does_not_promise_cli_token_on_v3() -> None:
    """В 3X-UI v3 CLI-команда не создаёт токены: подсказка не должна обещать."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "в v3 эта команда больше не создаёт токены" in text
    assert "apiTokens/create" in text


def test_installer_auto_corrects_web_base_path(tmp_path: Path) -> None:
    """Путь /panel из адресной строки — не webBasePath: установщик это ловит.

    Пробуем путь как есть, потом без него; «правильный» путь в 3X-UI отвечает
    401 (без токена), неверный — 404. Здесь curl подменён, чтобы проверить
    именно логику выбора.
    """
    script = INSTALL_SH.read_text(encoding="utf-8")

    def spell(name: str) -> str:
        start = script.index(f"{name}() {{")
        return script[start : script.index("\n}\n", start) + 3]

    helpers = (
        spell("collapse_slashes")
        + spell("panel_api_code")
        + spell("resolve_panel_base")
    )
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"{helpers}\n"
        'curl() { local url="${@: -1}"; '
        'case " $FAKE_OK " in *" $url "*) printf 401 ;; *) printf 404 ;; esac; }\n'
        'FAKE_OK="https://h:1/panel/api/server/status"\n'
        'printf "1:[%s]\\n" "$(resolve_panel_base https://h:1 panel)"\n'
        'FAKE_OK="https://h:1/secret/panel/api/server/status"\n'
        'printf "2:[%s]\\n" "$(resolve_panel_base https://h:1 secret)"\n'
        'printf "3:[%s]\\n" "$(resolve_panel_base https://h:1 "")"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, timeout=30, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "1:[]" in result.stdout
    assert "2:[secret]" in result.stdout
    assert "3:[]" in result.stdout


def test_dangerous_prefix_is_rejected(tmp_path: Path) -> None:
    """--prefix /etc не должен приводить к chmod -R и rm -rf системного каталога."""
    for bad in ("/etc", "/", "/usr", "/var"):
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--prefix", bad, "--print-source"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0, bad
        assert "Небезопасный --prefix" in result.stderr, (bad, result.stderr)


def test_dangerous_state_dir_is_rejected() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--print-source"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "STATE_DIR": "/etc"},
    )
    assert result.returncode != 0
    assert "Небезопасный STATE_DIR" in result.stderr


def test_sha256_disables_unverified_git_fallback() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "Git-fallback отключён" in text
    assert '[ -z "$SOURCE_SHA256" ]' in text


def test_init_does_not_source_env() -> None:
    """Текст из .env не должен исполняться init-скриптом."""
    text = (BASE_DIR / "install" / "stacksentry-bot.init").read_text(encoding="utf-8")
    assert ". '$PREFIX/.env'" not in text
    assert "set -a" not in text


def test_unit_state_dir_is_templated() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "s#/var/lib/stacksentry#${STATE_DIR}#g" in text


def test_set_env_keeps_env_readable_by_service(tmp_path: Path) -> None:
    """После set_env под umask 077 .env должен остаться 640, иначе бот не стартует."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("set_env() {")
    function = script[start : script.index("\n}\n", start) + 3]
    env_file = tmp_path / ".env"
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\numask 077\n"
        f'ENV_FILE="{env_file}"\n'
        'SERVICE_USER="$(id -un)"\nSERVICE_GROUP="$(id -gn)"\n'
        f"{function}\n"
        "set_env TEST value\n"
        'stat -c "%a" "$ENV_FILE"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "640"
    assert "TEST=value" in env_file.read_text(encoding="utf-8")


def test_service_group_is_not_assumed_to_equal_user() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'SERVICE_GROUP="$(id -gn "$SERVICE_USER"' in text
    assert "Group=${SERVICE_GROUP:-$SERVICE_USER}" in text


def test_project_renamed_with_legacy_migration() -> None:
    """Новое имя и автоматический перенос старой установки."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'REPO_NAME="${REPO_NAME:-stacksentry}"' in text
    assert "migrate_legacy_install()" in text
    assert "/opt/xui-monitor-bot" in text
    assert "/var/lib/xui-monitor" in text
    assert "/opt/uisentry" in text
    assert "/var/lib/uisentry" in text
    assert "перенесена и удалена" in text


def test_log_variant_buttons_are_allowed() -> None:
    """Кнопки «ещё строк»/«только ошибки» должны иметь правила sudoers."""
    keys = (
        "os_logs_more",
        "os_logs_errors",
        "xui_logs_more",
        "xui_logs_errors",
        "kernel_logs_more",
        "kernel_logs_errors",
        "dmesg_errors",
    )
    install = INSTALL_SH.read_text(encoding="utf-8")
    wrapper = (BASE_DIR / "install" / "stacksentry-ctl").read_text(encoding="utf-8")
    assert "run_command $key" in install
    for key in keys:
        assert key in install, key
        assert f'"{key}"' in wrapper, key


def test_installer_verifies_first_totp_code(tmp_path: Path) -> None:
    """Первый код проверяется до сохранения: иначе 2FA можно включить с опечаткой."""
    import sys

    from stacksentry.totp import totp_code

    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("verify_totp_code() {")
    end = script.index("\n}\n", start) + 3
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"SOURCE_DIR={BASE_DIR}\n"
        f"PYTHON_BIN={sys.executable}\n"
        f"{script[start:end]}\n"
        f'verify_totp_code "{secret}" "{totp_code(secret)}"\n'
        f'if verify_totp_code "{secret}" "000000"; then exit 9; fi\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr


def _run_configure_2fa(
    tmp_path: Path, env_text: str, settings: str, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    import shlex
    import sys

    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index("configure_2fa() {")
    end = script.index("\n}\n", start) + 3
    env_file = tmp_path / ".env"
    env_file.write_text(env_text, encoding="utf-8")
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"ENV_FILE={shlex.quote(str(env_file))}\n"
        "READ_FD=0\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        f"SOURCE_DIR={shlex.quote(str(BASE_DIR))}\n"
        "PKG_INSTALL=\n"
        "YELLOW=\nRESET=\nBOLD=\nGREEN=\n"
        "info(){ printf 'INFO:%s\\n' \"$1\"; }\n"
        "ok(){ printf 'OK:%s\\n' \"$1\"; }\n"
        "warn(){ printf 'WARN:%s\\n' \"$1\"; }\n"
        "ask(){ :; }\n"
        "show_totp_qr(){ printf 'QR:%s\\n' \"$1\"; }\n"
        "verify_totp_code(){ return 0; }\n"
        'env_get(){ sed -n "s/^$1=//p" "$ENV_FILE" | head -1; }\n'
        'set_env(){ grep -vE "^$1=" "$ENV_FILE" > "$ENV_FILE.tmp" || true; '
        'printf \'%s=%s\\n\' "$1" "$2" >> "$ENV_FILE.tmp"; '
        'mv "$ENV_FILE.tmp" "$ENV_FILE"; }\n'
        f"{script[start:end]}\n"
        f"{settings}\n"
        "configure_2fa\n"
        "printf 'RESULT_REQUIRED=%s\\n' \"$(env_get TOTP_REQUIRED)\"\n"
        "printf 'RESULT_SECRET=%s\\n' \"$(env_get TOTP_SECRET)\"\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(probe)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_2fa_disable_removes_secret(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    result = _run_configure_2fa(
        tmp_path,
        "TOTP_REQUIRED=1\nTOTP_SECRET=OLD\n",
        "DISABLE_2FA=1\nTWOFA_EXPLICIT=1\nINTERACTIVE=0\n"
        "ASSUME_YES=0\nCONFIG_COMPLETE=1\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_REQUIRED=0" in result.stdout
    assert "RESULT_SECRET=\n" in result.stdout
    assert "2FA отключена" in result.stdout


def test_2fa_update_without_choice_stays_undecided(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    result = _run_configure_2fa(
        tmp_path,
        "",
        "DISABLE_2FA=0\nTWOFA_EXPLICIT=0\nINTERACTIVE=0\n"
        "ASSUME_YES=1\nCONFIG_COMPLETE=1\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_REQUIRED=\n" in result.stdout
    assert "укажите --2fa или --no-2fa" in result.stdout


def test_2fa_enable_generates_secret_and_qr(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    result = _run_configure_2fa(
        tmp_path,
        "",
        "DISABLE_2FA=0\nTWOFA_EXPLICIT=1\nINTERACTIVE=1\n"
        "ASSUME_YES=1\nCONFIG_COMPLETE=1\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_REQUIRED=1" in result.stdout
    assert "RESULT_SECRET=\n" not in result.stdout
    assert "otpauth://totp/StackSentry:admin?" in result.stdout
    assert "QR:otpauth://totp/StackSentry:admin?" in result.stdout


def test_2fa_disabled_is_offered_again_on_interactive_update(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    result = _run_configure_2fa(
        tmp_path,
        "TOTP_REQUIRED=0\n",
        "DISABLE_2FA=0\nTWOFA_EXPLICIT=0\nINTERACTIVE=1\n"
        "ASSUME_YES=0\nCONFIG_COMPLETE=1\n",
        stdin="y\n\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_REQUIRED=1" in result.stdout
    assert "RESULT_SECRET=\n" not in result.stdout
    assert "QR:otpauth://totp/StackSentry:admin?" in result.stdout


def test_installer_supports_2fa() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "--2fa" in text
    assert "--no-2fa" in text
    assert "otpauth://totp/" in text
    menu = (BASE_DIR / "install" / "stacksentry-menu").read_text(encoding="utf-8")
    assert "cmd_totp" in menu
    assert "TOTP secret" in menu
    assert "Установить 2FA (TOTP) для Mini App и опасных действий?" in text
    assert "2FA ВКЛЮЧЕНА" in text
    assert "verify_totp_code()" in text
    assert "show_totp_qr()" in text
    assert "show_totp_qr()" in menu
    assert "2FA отключена — TOTP не используется." in menu
    assert (
        "2FA не настроена — выберите: sudo bash ${PREFIX}/install.sh --2fa"
        " (или --no-2fa)"
    ) in text
    assert "Код подтверждён" in text
    assert "26) cmd_totp" in menu
    assert "27) cmd_webapp" in menu
    assert "webapp|mini-app|miniapp) cmd_webapp" in menu
    assert "Показать TOTP-секрет" in menu
    assert "Статус Mini App" in menu
    # После обновления существующей установки недостающие компоненты спрашиваем.
    assert "Установить Mini App (панель в Telegram)?" in text
    assert "Установить веб-терминал (TOTP + root)?" in text


def test_installer_can_enable_https_without_a_domain() -> None:
    """Mini App должен подниматься из коробки: sslip.io + Caddy + Let's Encrypt."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "webapp_auto_https()" in text
    assert "webapp_https_preflight()" in text
    assert "webapp_wizard()" in text
    assert "webapp_fallback()" in text
    assert "webapp_ask_manual_origin()" in text
    assert "webapp_set_terminal_origin()" in text
    assert "Как открывать Mini App?" in text
    assert "Повторить" in text
    assert "Открыть терминал внутри Mini App?" in text
    assert "reverse_proxy 127.0.0.1:8788" in text
    assert "term.${WEBAPP_PREFLIGHT_HOST}" in text
    assert "--terminal-auto" in text
    assert "/etc/stacksentry/botfather.txt" in text
    assert 'local caddyfile="/etc/caddy/Caddyfile"' in text
    assert ".stacksentry-managed" in text
    assert "Заменить конфиг Caddy для StackSentry?" in text
    assert "Без этого шага кнопка Mini App не появится." in text
    assert "Отправьте ровно эту строку" in text
    assert "detect_public_ipv4" in text
    assert ".sslip.io" in text
    assert "Let's Encrypt" in text
    assert "$PKG_INSTALL caddy" in text
    assert "webapp_port_is_free 80" in text
    assert "webapp_port_is_free 443" in text
    assert "8443" in text
    assert "--no-webapp-auto" in text
    assert '_webapp_domain="${_webapp_domain%%:*}"' in text
    assert "systemctl disable --now caddy" in text
    assert "StackSentry Mini App" in text
    menu = (BASE_DIR / "install" / "stacksentry-menu").read_text(encoding="utf-8")
    assert 'domain="${domain%%:*}"' in menu


def test_flags_apply_to_complete_env_and_terminal_secrets_preserved() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "Флаги и обязательная проверка 2FA работают и на уже готовом .env" in text
    assert "--no-root-terminal" in text
    assert "_old_totp" in text
    assert "_old_root" in text
    assert "TERMINAL_SHELL_USER" in text
    assert "stacksentry-shell" in text


def test_self_update_is_exposed_through_the_narrow_sudoers() -> None:
    sample = (BASE_DIR / "install" / "stacksentry-sudoers").read_text(encoding="utf-8")
    assert "stacksentry-ctl update_bot" in sample
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "update_bot" in text
    # Установщик пишет PREFIX для ctl: self-update должен работать и при --prefix.
    assert "/etc/stacksentry.conf" in text
    # Метаданные для проверки обновлений.
    assert ".stacksentry-install" in text


def test_webapp_has_separate_user_and_state() -> None:
    """Веб-слой не должен работать под пользователем бота и писать его базу."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'WEBAPP_USER="${WEBAPP_USER:-stacksentry-webapp}"' in text
    assert "User=$WEBAPP_USER" in text
    assert "SupplementaryGroups=${SERVICE_GROUP:-$SERVICE_USER}" in text
    assert "Environment=WEBAPP_STATE_PATH=$WEBAPP_HOME/state.db" in text
    assert "Environment=STATE_PATH=$WEBAPP_HOME/state.db" not in text
    assert "ReadWritePaths=$WEBAPP_HOME" in text
    assert 'useradd --system --no-create-home --home-dir "$WEBAPP_HOME"' in text


def test_installer_does_not_put_secrets_in_curl_argv() -> None:
    """Токены и подписанные URL передаются curl через stdin-конфиг."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "curl_secure()" in text
    assert "curl_secure_form()" in text
    assert 'TG_PROBE="$(curl -sS' not in text
    assert 'TG_RESULT="$(curl -sS' not in text
    assert '-H "Authorization: Bearer $PANEL_TOKEN"' not in text
    assert '-H "Token: $PANEL_TOKEN"' not in text
    assert '-H "Hiddify-API-Key: $PANEL_TOKEN"' not in text
    assert '--data-urlencode "password=$PANEL_PASSWORD_VALUE"' not in text


def test_self_update_works_without_manual_pin() -> None:
    """Массовой установке не нужен ручной UPDATE_SHA256; пин опционален."""
    installer = INSTALL_SH.read_text(encoding="utf-8")
    ctl = (BASE_DIR / "install" / "stacksentry-ctl").read_text(encoding="utf-8")
    menu = (BASE_DIR / "install" / "stacksentry-menu").read_text(encoding="utf-8")

    assert "Обновление без контрольной суммы запрещено" not in installer
    assert "не задан UPDATE_SHA256" not in ctl
    assert "не задан UPDATE_SHA256" not in menu
    assert "unpinned" in ctl
    assert "Пин SHA256 не задан" in menu
    assert "--sha256" in ctl and "--sha256" in menu
