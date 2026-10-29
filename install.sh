#!/usr/bin/env bash
#
# StackSentry — установка на Linux-сервер одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh | sudo bash
#
# Вопросы читаются с терминала (/dev/tty), поэтому пайп не мешает диалогу.
# Альтернатива, как у 3X-UI:  sudo bash <(curl -fsSL .../install.sh)
# Удаление:                   sudo bash install.sh --uninstall
# Меню:                       sudo bash install.sh --menu
#
# Скрипт сам скачивает исходники, ставит зависимости, задаёт несколько вопросов
# (токен от @BotFather, ваш Telegram ID, адрес и API-токен панели), создаёт
# непривилегированного пользователя, включает автозапуск через systemd и в
# конце проверяет, что всё действительно работает.
#
# Что нужно до старта:
#   • токен Telegram-бота от @BotFather (BotFather → /newbot);
#   • ваш Telegram ID (узнать у @userinfobot);
#   • адрес панели 3X-UI и её API-токен (выпускается на сервере).
#
# Повторный запуск обновляет бота и сохраняет ваш .env, токены и состояние.
#
set -euo pipefail

# Всё, что создаёт установщик, включает токены: не даём файлам родиться
# читаемыми для посторонних (права уточняются ниже по месту).
umask 077

# Откуда читать ответы на вопросы. При установке одной строкой через пайп
# (`curl … | sudo bash`) stdin занят самим скриптом; без этого блока вопросы
# молча пропускались бы и установка шла на значениях по умолчанию. Поэтому
# при недоступном stdin спрашиваем терминал напрямую (/dev/tty). Вариант
# `sudo bash <(curl …)` работает и без этого: там stdin — уже терминал.
READ_FD=0
INTERACTIVE=0
if [ -t 0 ]; then
    INTERACTIVE=1
elif { exec 3</dev/tty; } 2>/dev/null; then
    READ_FD=3
    INTERACTIVE=1
fi

REPO_OWNER="${REPO_OWNER:-Hijackery}"
REPO_NAME="${REPO_NAME:-stacksentry}"
REPO_REF="${REPO_REF:-main}"
REPO_URL="${REPO_URL:-}"
TARBALL_URL="${TARBALL_URL:-}"
SOURCE_SHA256="${SOURCE_SHA256:-}"
REPO_SLUG="${REPO:-}"

# Приводит адрес репозитория к согласованному виду (owner/name, REPO_URL,
# TARBALL_URL). Отдельная функция: опции --repo/--ref разбираются уже после
# чтения переменных окружения.
resolve_repo() {
    if [ -n "$REPO_SLUG" ]; then
        case "$REPO_SLUG" in
            */*) REPO_OWNER="${REPO_SLUG%%/*}"; REPO_NAME="${REPO_SLUG##*/}" ;;
            *) fail "--repo ожидает owner/name, получено: $REPO_SLUG" ;;
        esac
    fi

    [ -n "$REPO_URL" ] || REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}"

    # Явный REPO_URL должен определять и откуда скачиваем: иначе подмена молча
    # игнорируется и скрипт тянет заведомо несуществующий OWNER/...
    case "$REPO_URL" in
        *github.com/*/*)
            _url_owner="$(printf '%s' "$REPO_URL" | sed -E 's#.*github\.com/([^/]+)/([^/]+).*#\1#')"
            _url_repo="$(printf '%s' "$REPO_URL" | sed -E 's#.*github\.com/([^/]+)/([^/]+).*#\2#')"
            _url_repo="${_url_repo%.git}"
            if [ -n "$_url_owner" ] && [ -n "$_url_repo" ]; then
                REPO_OWNER="$_url_owner"
                REPO_NAME="$_url_repo"
            fi
            ;;
    esac

    [ -n "$TARBALL_URL" ] \
        || TARBALL_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REPO_REF}"
}

# Ссылки на источник по порядку попыток: указанная ветка/тег, затем обычные
# main и master — у нового репозитория имя основной ветки может быть любым.
source_candidates() {
    printf '%s\n' "$TARBALL_URL"
    case "$TARBALL_URL" in
        *codeload.github.com/*/tar.gz/refs/heads/*)
            _cand_base="${TARBALL_URL%/*}"
            [ "$REPO_REF" = "main" ] || printf '%s\n' "${_cand_base}/main"
            [ "$REPO_REF" = "master" ] || printf '%s\n' "${_cand_base}/master"
            ;;
    esac
}

repo_is_placeholder() {
    [ "$REPO_OWNER" = "OWNER" ] || [ "$REPO_NAME" = "REPO" ]
}

# Проверка целостности архива: с SOURCE_SHA256 установка привязана к конкретной
# версии, и подмена архива (прокси, форк, скомпрометированный CDN) не пройдёт.
verify_archive() {
    [ -n "${SOURCE_SHA256:-}" ] || return 0
    command -v sha256sum >/dev/null 2>&1 \
        || fail "Задан SOURCE_SHA256, но утилита sha256sum не найдена (пакет coreutils)."
    _actual="$(sha256sum "$1" | awk '{print $1}')"
    if [ "$_actual" != "$SOURCE_SHA256" ]; then
        warn "Контрольная сумма не совпала: ожидалось $SOURCE_SHA256, получено $_actual"
        return 1
    fi
    ok "Контрольная сумма архива совпала"
    return 0
}


# ---------------------------------------------------------------- оформление

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

STEP=0
FAILURES=0
CHECKS=0

step() {
    STEP=$((STEP + 1))
    printf '\n%s[%d/%d] %s%s\n' "$BOLD$BLUE" "$STEP" "$TOTAL_STEPS" "$1" "$RESET"
}
# Секреты (токены, пароли, подписанные URL) не должны попадать в argv:
# /proc/<pid>/cmdline и ps видны другим пользователям. Передаём URL и
# заголовки curl через stdin-конфиг (-K -), оставляя в argv только флаги.
curl_cfg_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\r\n'
}

curl_secure() {
    # curl_secure "Заголовок" "URL" [аргументы curl...]
    local _header="$1" _url="$2"
    shift 2
    {
        [ -n "$_header" ] \
            && printf 'header = "%s"\n' "$(curl_cfg_escape "$_header")"
        printf 'url = "%s"\n' "$(curl_cfg_escape "$_url")"
    } | curl -K - "$@"
}

curl_secure_form() {
    # curl_secure_form "URL" "логин" "пароль" [аргументы curl...]
    local _url="$1" _user="$2" _password="$3"
    shift 3
    {
        printf 'url = "%s"\n' "$(curl_cfg_escape "$_url")"
        printf 'data-urlencode = "username=%s"\n' "$(curl_cfg_escape "$_user")"
        printf 'data-urlencode = "password=%s"\n' "$(curl_cfg_escape "$_password")"
        printf 'data-urlencode = "grant_type=password"\n'
    } | curl -K - "$@"
}

info()  { printf '      %s\n' "$1"; }
ok()    { printf '      %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()  { printf '      %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
bad()   { printf '      %s✗%s %s\n' "$RED" "$RESET" "$1"; }
fail()  { printf '\n%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }
ask()   { printf '      %s' "$1"; }
section() { printf '\n%s%s%s\n' "$BOLD$BLUE" "$1" "$RESET"; }

# Проверка первого кода TOTP до сохранения секрета: опечатка в приложении
# иначе обнаруживается только при входе в Mini App.
verify_totp_code() {
    PYTHONPATH="$SOURCE_DIR" "$PYTHON_BIN" - "$1" "$2" <<'PY'
import sys
from stacksentry.totp import verify_totp
raise SystemExit(0 if verify_totp(sys.argv[1], sys.argv[2]) else 1)
PY
}

# Печатает QR для otpauth в терминале. qrencode ставится только когда 2FA
# действительно включают; без него остаётся рабочий otpauth.
show_totp_qr() {
    local uri="$1" kind="UTF8"
    if ! command -v qrencode >/dev/null 2>&1 && [ -n "$PKG_INSTALL" ]; then
        info "Ставлю qrencode для QR-кода…"
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive $PKG_INSTALL qrencode >/dev/null 2>&1 || true
    fi
    if command -v qrencode >/dev/null 2>&1; then
        if qrencode --help 2>&1 | grep -q 'ANSIUTF8'; then
            kind="ANSIUTF8"
        fi
        printf '\n'
        qrencode -t "$kind" -m 2 -s 1 -o - "$uri" \
            || warn "Не удалось нарисовать QR — отсканируйте otpauth вручную."
        printf '\n'
    else
        warn "qrencode не установлен — отсканируйте otpauth вручную."
    fi
}

# Опциональная 2FA: выбор пользователя. Если включена — обязательна для
# Mini App и опасных действий бота. Если выключена — секрет удаляется,
# TOTP не спрашивается нигде.
configure_2fa() {
    local current secret enable uri answer attempt
    current="$(env_get TOTP_REQUIRED)"
    secret="$(env_get TOTP_SECRET)"

    if [ "$DISABLE_2FA" -eq 1 ]; then
        enable=0
    elif [ "$TWOFA_EXPLICIT" -eq 1 ]; then
        enable=1
    elif [ -n "$secret" ] && [ "$current" = "1" ]; then
        enable=1
    elif [ -n "$secret" ] && [ -z "$current" ]; then
        enable=1
    elif [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        ask "Установить 2FA (TOTP) для Mini App и опасных действий? [Y/n] "
        read -r answer <&$READ_FD || answer=""
        case "$answer" in
            [nNнН]*) enable=0 ;;
            *) enable=1 ;;
        esac
    elif [ "$current" = "0" ]; then
        enable=0
    elif [ "$CONFIG_COMPLETE" -eq 1 ]; then
        # Обновление без TTY: не решаем за пользователя молча. Самопроверка
        # попросит явно указать --2fa или --no-2fa.
        info "2FA не настроена — укажите --2fa или --no-2fa."
        return 0
    else
        enable=0
    fi

    if [ "$enable" -eq 0 ]; then
        set_env TOTP_REQUIRED 0
        set_env TOTP_SECRET ""
        info "2FA отключена — TOTP не используется."
        return 0
    fi

    if [ -n "$secret" ] && [ "$current" = "1" ] && [ "$TWOFA_EXPLICIT" -eq 0 ]; then
        info "2FA уже включена."
        return 0
    fi

    secret="$(env_get TOTP_SECRET)"
    if [ -z "$secret" ]; then
        secret="$("$PYTHON_BIN" -c \
            'import base64,secrets; print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="))')"
        printf '\n%s+------------------------------------------------------------+%s\n' "$YELLOW" "$RESET"
        printf '%s| %-58s |%s\n' "$YELLOW" "2FA ВКЛЮЧЕНА — ДОБАВЬТЕ СЕКРЕТ В АУТЕНТИФИКАТОР" "$RESET"
        printf '+------------------------------------------------------------+\n'
        printf '| %-58s |\n' "1. Откройте Google Authenticator / Aegis / 1Password."
        printf '| %-58s |\n' "2. Отсканируйте QR ниже или введите секрет вручную."
        printf '| %-58s |\n' "3. Код понадобится для Mini App и опасных действий."
        printf '+------------------------------------------------------------+\n'
        printf '| %-58s |\n' "TOTP secret:"
        printf '| %s%s%-58s%s |\n' "$BOLD" "$GREEN" "$secret" "$RESET"
        printf '+------------------------------------------------------------+\n\n'
    else
        info "2FA уже настроена."
    fi
    uri="otpauth://totp/StackSentry:admin?secret=${secret}&issuer=StackSentry"
    printf 'otpauth: %s\n' "$uri"
    if [ "$INTERACTIVE" -eq 1 ]; then
        show_totp_qr "$uri"
    fi

    if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        attempt=0
        while [ "$attempt" -lt 3 ]; do
            ask "Введите 6-значный код из аутентификатора (Enter — пропустить): "
            read -r answer <&$READ_FD || answer=""
            if [ -z "$answer" ]; then
                warn "Код не проверен — добавьте секрет в аутентификатор вручную."
                break
            fi
            if verify_totp_code "$secret" "$answer"; then
                ok "Код подтверждён — 2FA работает."
                break
            fi
            attempt=$((attempt + 1))
            if [ "$attempt" -lt 3 ]; then
                warn "Код не подошёл. Проверьте время на телефоне и попробуйте снова."
            else
                warn "Код не подтверждён — проверьте секрет через меню."
            fi
        done
    fi
    set_env TOTP_SECRET "$secret"
    set_env TOTP_REQUIRED 1
    ok "2FA включена: Mini App и опасные действия требуют код"
}

# check_ok / check_fail — строки самопроверки в конце установки
check_ok()   { CHECKS=$((CHECKS + 1)); ok "$1"; }
check_fail() { CHECKS=$((CHECKS + 1)); FAILURES=$((FAILURES + 1)); bad "$1"; }
check_note() { printf '      %s•%s %s\n' "$DIM" "$RESET" "$1"; }

# Печатает IP-адреса хоста через пробел. `hostname -I` есть не везде
# (BusyBox и урезанные системы), поэтому есть запасной путь через `ip`.
#: Приводит адрес к каноническому виду: без пробелов и завершающих слешей.
#: Без этого вставленный «https://host:5580/» и путь «/panel/» давали в проверке
#: двойной слеш, а бот потом строил кривой API-адрес.
normalize_url() {
    printf '%s' "$1" | tr -d '[:space:]' | sed -E 's#/*$##'
}

#: Путь webBasePath: без пробелов и слешей по краям (пусто — значит не задан).
normalize_path() {
    printf '%s' "$1" | tr -d '[:space:]' | sed -E 's#^/*##; s#/*$##'
}

#: Отделяет путь от адреса, если его вставили вместе с хостом:
#: «https://host:5580/panel/» → «https://host:5580 panel». Так делает большинство
#: людей (копируют адрес из браузера), а потом панель отвечает 404, потому что
#: путь попадает в адрес API как «/panel/panel/api».
split_panel_address() {
    _address="$1"
    _scheme=""
    case "$_address" in
        *://*) _scheme="${_address%%://*}://" ;;
    esac
    _rest="${_address#"$_scheme"}"
    _host="${_rest%%/*}"
    _path="${_rest#"$_host"}"
    printf '%s %s' "${_scheme}${_host}" "$(normalize_path "$_path")"
}

#: Сворачивает повторные слеши в адресе («https://host//» → «https://host/»),
#: не трогая «://» в схеме. Последняя страховка перед проверкой панели.
collapse_slashes() {
    printf '%s' "$1" | sed -E 's#([^:])//+#\1/#g'
}

detect_ips() {
    local v4=""
    if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1; then
        # Только IPv4: IPv6-литерал без скобок даёт невалидный URL.
        v4="$(hostname -I 2>/dev/null | tr ' ' '\n' \
            | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | tr '\n' ' ' \
            | sed 's/ *$//')"
    fi
    if [ -z "$v4" ] && command -v ip >/dev/null 2>&1; then
        v4="$(ip -4 -o addr show scope global 2>/dev/null \
            | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ' | sed 's/ *$//')"
    fi
    printf '%s' "$v4"
}

cleanup() {
    [ -n "${TMP_DIR:-}" ] && [ -d "${TMP_DIR:-}" ] && rm -rf "$TMP_DIR"
    return 0
}
trap cleanup EXIT

# ------------------------------------------------------------------ параметры

PREFIX="${PREFIX:-/opt/stacksentry}"
SERVICE_USER="${SERVICE_USER:-stacksentry}"
# Каталог состояния (база SQLite и файл лога) живёт отдельно от кода:
# код принадлежит root, состояние — сервисному пользователю.
STATE_DIR="${STATE_DIR:-/var/lib/stacksentry}"
# Mini App — отдельный пользователь без sudo: компрометация веб-слоя не даёт
# доступ к обёртке root-операций и ключам терминала.
WEBAPP_USER="${WEBAPP_USER:-stacksentry-webapp}"
WEBAPP_HOME="${WEBAPP_HOME:-/var/lib/stacksentry-webapp}"
SERVICE_NAME="stacksentry-bot"
WRAPPER_PATH="${WRAPPER_PATH:-/usr/local/bin/stacksentry-ctl}"
MENU_PATH="${MENU_PATH:-/usr/local/bin/stacksentry-menu}"
DOCTOR_PATH="${DOCTOR_PATH:-/usr/local/bin/stacksentry-doctor}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SUDOERS_PATH="/etc/sudoers.d/${SERVICE_NAME}"
START_SERVICE=1
TERMINAL=0
TERMINAL_EXPLICIT=0
TERMINAL_ALLOW_ROOT=0
TERMINAL_ORIGIN=""
TERMINAL_ORIGIN_EXPLICIT=0
TERMINAL_PUBLIC=0
ROOT_TERMINAL_EXPLICIT=0
WEBAPP=0
WEBAPP_EXPLICIT=0
WEBAPP_ORIGIN_EXPLICIT=0
WEBAPP_AUTO_HTTPS="${WEBAPP_AUTO_HTTPS:-1}"
WEBAPP_AUTO_ORIGIN=""
WEBAPP_AUTO_TERMINAL_ORIGIN=""
WEBAPP_CADDY_ADOPT=0
WEBAPP_PREFLIGHT_OK=0
WEBAPP_PREFLIGHT_IP=""
WEBAPP_PREFLIGHT_HOST=""
WEBAPP_PREFLIGHT_PORT=""
WEBAPP_PREFLIGHT_ORIGIN=""
WEBAPP_PREFLIGHT_TERMINAL_ORIGIN=""
WEBAPP_PREFLIGHT_REASON=""
WEBAPP_PREFLIGHT_CADDY_INSTALLED=0
PANEL_MODE_EXPLICIT=0
DISABLE_2FA=0
TWOFA_EXPLICIT=0
PANEL_MODE="auto"
WEBAPP_ORIGIN=""
INIT="${INIT:-}"
ASSUME_YES=0
KEEP_CONFIG=1
UNINSTALL=0
KEEP_STATE=0
MENU=0
PRINT_SOURCE=0
UPDATE=0

usage() {
    cat <<EOF
${BOLD}Установка StackSentry${RESET}

  sudo bash install.sh [опции]

Опции:
  --prefix DIR     каталог установки (по умолчанию: /opt/stacksentry)
  --user NAME      системный пользователь бота (по умолчанию: stacksentry)
  --tarball URL    откуда скачать исходники (tar.gz), минуя репозиторий по умолчанию
  --repo OWNER/NAME  репозиторий на GitHub (краткая форма вместо REPO_OWNER/REPO_NAME)
  --ref BRANCH     ветка или тег репозитория (по умолчанию main)
  --print-source   показать, откуда будут взяты исходники, и выйти
  --sha256 HASH    сверить скачанный архив с контрольной суммой (доп. защита)
  --update         обновление; с --sha256 проверяет пин, без него — обновляет ref
  --menu           меню: установить/обновить, удалить, справка
                   (после установки для правок настроек: sudo stacksentry-menu)
  --uninstall      удалить бота целиком: служба, sudoers, обёртка, меню, код,
                   состояние (/var/lib/stacksentry) и системный пользователь
  --purge          то же самое (синоним, оставлен для совместимости)
  --keep-state     не трогать состояние и пользователя
  --yes            без вопросов: берём значения из окружения или готового .env
  --force-config   перезаписать существующий .env значениями по умолчанию
  --no-start       установить, но не запускать сервис
  --init TYPE      менеджер служб: systemd, openrc или sysv (по умолчанию — авто)
  --terminal       поставить отдельный веб-терминал (TOTP, только localhost)
  --root-terminal  то же + разрешить root-сессию (sudo -i) через sudoers
  --no-root-terminal  выключить root-сессию, остальное сохранить
  --no-terminal    не ставить терминал (на обновлении оставить как есть)
  --terminal-origin URL  публичный HTTPS-адрес терминала (иначе SSH-туннель)
  --terminal-auto  открыть терминал внутри Mini App автоматически
  --2fa            включить 2FA (TOTP) для опасных действий
  --no-2fa         выключить 2FA (секрет сохраняется)
  --panel          настроить API панели (по умолчанию — автоопределение)
  --no-panel       не настраивать API панели (только мониторинг служб)
  --webapp         поставить read-only Mini App (127.0.0.1:8787)
  --no-webapp      не ставить Mini App (на обновлении оставить как есть)
  --webapp-origin URL  публичный HTTPS-адрес Mini App (для кнопки в Telegram)
  --no-webapp-auto     не настраивать HTTPS автоматически (sslip.io + Caddy)
  --help           эта справка

  При обычной интерактивной установке Mini App, терминал, root-сессия и 2FA
  включаются по умолчанию — отдельные флаги нужны только для автоматизации.

Переменные окружения (для --yes и unattended-установки):
  REPO, REPO_OWNER, REPO_NAME, REPO_REF, REPO_URL, TARBALL_URL, GIT_URL,
  SOURCE_SHA256, TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS, TELEGRAM_API_URL,
  PANEL_URL, PANEL_BASE_PATH, PANEL_API_TOKEN, SERVICES

${BOLD}English${RESET}
  Install: bash <(curl -Ls .../main/install.sh)
  Options:  --prefix DIR · --user NAME · --repo owner/name · --ref BRANCH
            --tarball URL · --sha256 HASH · --init systemd|openrc|sysv
            --terminal · --no-terminal · --root-terminal · --no-root-terminal
            --terminal-origin URL · --terminal-auto · --2fa · --no-2fa
            --panel · --no-panel · --webapp · --no-webapp · --webapp-origin URL
            --no-webapp-auto · --print-source · --menu · --uninstall · --purge · --no-start
            --force-config · --yes · --help
  Docs:     README.en.md (English) · README.md (Русский)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)       PREFIX="${2:?--prefix требует путь}"; shift 2 ;;
        --user)         SERVICE_USER="${2:?--user требует имя}"; shift 2 ;;
        --tarball)      TARBALL_URL="${2:?--tarball требует URL}"; shift 2 ;;
        --repo)         REPO_SLUG="${2:?--repo требует owner/name}"; shift 2 ;;
        --ref)          REPO_REF="${2:?--ref требует ветку или тег}"; shift 2 ;;
        --print-source) PRINT_SOURCE=1; shift ;;
        --sha256)       SOURCE_SHA256="${2:?--sha256 требует хеш}"; shift 2 ;;
        --update)       UPDATE=1; ASSUME_YES=1; shift ;;
        --menu|-m)      MENU=1; shift ;;
        --uninstall)    UNINSTALL=1; shift ;;
        --purge)        UNINSTALL=1; shift ;;   # синоним: удаление и так полное
        --keep-state)   KEEP_STATE=1; shift ;;
        --yes|-y)       ASSUME_YES=1; shift ;;
        --force-config) KEEP_CONFIG=0; shift ;;
        --no-start)     START_SERVICE=0; shift ;;
        --init)         INIT="${2:?--init требует systemd|openrc|sysv}"; shift 2 ;;
        --terminal)     TERMINAL=1; TERMINAL_EXPLICIT=1; shift ;;
        --no-terminal)  TERMINAL=0; TERMINAL_EXPLICIT=1; shift ;;
        --root-terminal) TERMINAL=1; TERMINAL_EXPLICIT=1; TERMINAL_ALLOW_ROOT=1; ROOT_TERMINAL_EXPLICIT=1; shift ;;
        --no-root-terminal) TERMINAL=1; TERMINAL_EXPLICIT=1; TERMINAL_ALLOW_ROOT=0; ROOT_TERMINAL_EXPLICIT=1; shift ;;
        --terminal-origin) TERMINAL=1; TERMINAL_EXPLICIT=1; TERMINAL_ORIGIN="${2:?--terminal-origin требует URL}"; TERMINAL_ORIGIN_EXPLICIT=1; TERMINAL_PUBLIC=1; shift 2 ;;
        --terminal-auto) TERMINAL=1; TERMINAL_EXPLICIT=1; TERMINAL_PUBLIC=1; shift ;;
        --2fa)          TWOFA_EXPLICIT=1; shift ;;
        --no-2fa)       DISABLE_2FA=1; TWOFA_EXPLICIT=1; shift ;;
        --panel)        PANEL_MODE=1; PANEL_MODE_EXPLICIT=1; shift ;;
        --no-panel)     PANEL_MODE=0; PANEL_MODE_EXPLICIT=1; shift ;;
        --webapp)       WEBAPP=1; WEBAPP_EXPLICIT=1; shift ;;
        --no-webapp)    WEBAPP=0; WEBAPP_EXPLICIT=1; shift ;;
        --webapp-origin) WEBAPP=1; WEBAPP_EXPLICIT=1; WEBAPP_ORIGIN="${2:?--webapp-origin требует URL}"; WEBAPP_ORIGIN_EXPLICIT=1; shift 2 ;;
        --no-webapp-auto) WEBAPP_AUTO_HTTPS=0; shift ;;
        --webapp-auto)  WEBAPP_AUTO_HTTPS=1; shift ;;
        --help|-h)      usage; exit 0 ;;
        *) fail "Неизвестная опция: $1 (см. --help)" ;;
    esac
done

# --- защита от опасного --prefix/STATE_DIR ---------------------------------
# Без этого `--prefix /etc` приводил к chmod -R по системному каталогу, а
# `--uninstall` — к rm -rf. Разрешаем только предсказуемые места установки.
canon_path() { readlink -m "$1" 2>/dev/null || printf '%s' "$1"; }
is_safe_prefix() {
    case "$1" in
        /opt/?*|/usr/local/?*|/srv/?*|/var/opt/?*|/home/?*/?*) return 0 ;;
        *) return 1 ;;
    esac
}
is_safe_state() {
    case "$1" in
        /var/lib/?*|/var/opt/?*|/opt/?*|/srv/?*|/home/?*/?*) return 0 ;;
        *) return 1 ;;
    esac
}
PREFIX="$(canon_path "$PREFIX")"
STATE_DIR="$(canon_path "$STATE_DIR")"
if [ "${ALLOW_UNSAFE_PREFIX:-0}" != "1" ]; then
    is_safe_prefix "$PREFIX" || fail "Небезопасный --prefix: $PREFIX
  Разрешено: /opt/..., /usr/local/..., /srv/..., /var/opt/..., /home/<user>/...
  Осознанно разрешить: ALLOW_UNSAFE_PREFIX=1"
    is_safe_state "$STATE_DIR" || fail "Небезопасный STATE_DIR: $STATE_DIR
  Разрешено: /var/lib/..., /var/opt/..., /opt/..., /srv/..., /home/<user>/..."
fi

resolve_repo

# Обновление исполняет новый код от root. Пин SHA256 остаётся доступным
# (UPDATE_SHA256/--sha256), но для массовых установок не обязателен: без него
# обновляем выбранный ref по HTTPS. Оператор, которому нужна защита от подмены
# архива, задаёт пин — тогда проверка fail-closed.
if [ "$UPDATE" -eq 1 ] && [ -z "$SOURCE_SHA256" ]; then
    warn "Пин SHA256 не задан: обновление из ref=$REPO_REF без проверки хеша."
fi

# Диагностика без правки системы: удобно проверять адреса и тесты.
if [ "$PRINT_SOURCE" -eq 1 ]; then
    printf 'REPO_URL=%s\n' "$REPO_URL"
    printf 'REPO_REF=%s\n' "$REPO_REF"
    printf 'TARBALL_URL=%s\n' "$TARBALL_URL"
    printf 'GIT_URL=%s\n' "${GIT_URL:-$REPO_URL}"
    printf 'CANDIDATES=%s\n' "$(source_candidates | tr '\n' ' ')"
    if repo_is_placeholder; then
        printf 'placeholder=yes\n'
    else
        printf 'placeholder=no\n'
    fi
    exit 0
fi

# -------------------------------------------------------- удаление / меню

uninstall() {
    [ "$(id -u)" -eq 0 ] || fail "Запускать от root: sudo bash install.sh --uninstall"

    section "Удаление бота $SERVICE_NAME"
    if [ "$ASSUME_YES" -eq 0 ] && [ "$INTERACTIVE" -eq 1 ]; then
        ask "Удалить бота целиком (код, база, пользователь)? [y/N]"
        read -r answer <&$READ_FD || answer=""
        case "$answer" in
            [yYдД]*) ;;
            *) info "Отменено, ничего не тронуто"; return 0 ;;
        esac
    fi

    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    rm -f "$UNIT_PATH" \
        "/etc/systemd/system/multi-user.target.wants/${SERVICE_NAME}.service" \
        "/etc/systemd/system/stacksentry-apt-update.service" \
        "/etc/systemd/system/stacksentry-apt-upgrade.service" \
        "/etc/systemd/system/stacksentry-apt-phased.service" \
        "/etc/systemd/system/stacksentry-release-upgrade.service" \
        "/etc/systemd/system/stacksentry-self-update.service"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi

    # Служба могла быть поставлена init-скриптом (OpenRC/SysV) — убираем и её.
    INIT_SCRIPT="/etc/init.d/${SERVICE_NAME}"
    if [ -f "$INIT_SCRIPT" ]; then
        "$INIT_SCRIPT" stop >/dev/null 2>&1 || true
        rm -f "$INIT_SCRIPT"
        if command -v rc-update >/dev/null 2>&1; then
            rc-update del "$SERVICE_NAME" default >/dev/null 2>&1 || true
        fi
        if command -v update-rc.d >/dev/null 2>&1; then
            update-rc.d -f "$SERVICE_NAME" remove >/dev/null 2>&1 || true
        fi
        if command -v chkconfig >/dev/null 2>&1; then
            chkconfig --del "$SERVICE_NAME" >/dev/null 2>&1 || true
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi

    rm -f "$SUDOERS_PATH" "$WRAPPER_PATH" "$MENU_PATH" "$DOCTOR_PATH" /etc/stacksentry.conf

    # Терминал (если ставился) — отдельная служба, пользователь и секреты.
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now stacksentry-terminal >/dev/null 2>&1 || true
        systemctl disable --now stacksentry-webapp >/dev/null 2>&1 || true
    fi
    rm -f /etc/systemd/system/stacksentry-terminal.service \
        /etc/systemd/system/stacksentry-webapp.service \
        /etc/sudoers.d/stacksentry-terminal \
        /etc/stacksentry/terminal.env /etc/stacksentry/terminal-secrets.env
    # Caddy мог быть поставлен только ради авто-HTTPS Mini App.
    if [ -f /etc/caddy/Caddyfile ] \
        && grep -q 'StackSentry Mini App' /etc/caddy/Caddyfile 2>/dev/null; then
        if [ -f /etc/caddy/Caddyfile.stacksentry.bak ]; then
            mv -f /etc/caddy/Caddyfile.stacksentry.bak /etc/caddy/Caddyfile \
                >/dev/null 2>&1 || true
        else
            rm -f /etc/caddy/Caddyfile
        fi
        rm -f /etc/caddy/.stacksentry-managed
        if command -v systemctl >/dev/null 2>&1; then
            systemctl disable --now caddy >/dev/null 2>&1 || true
        fi
    fi
    rm -rf /etc/stacksentry /var/lib/stacksentry-term /var/lib/stacksentry-shell
    for _term_user in stacksentry-term stacksentry-shell; do
        if id "$_term_user" >/dev/null 2>&1; then
            userdel "$_term_user" >/dev/null 2>&1 || true
        fi
    done
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi

    rm -rf "$PREFIX"

    if [ "$KEEP_STATE" -eq 1 ]; then
        warn "По --keep-state оставлены база и пользователи: $STATE_DIR, $SERVICE_USER, $WEBAPP_USER"
    else
        # По умолчанию вычищаем под ноль: база и логи больше не нужны.
        rm -rf "$STATE_DIR" "$WEBAPP_HOME"
        if id "$WEBAPP_USER" >/dev/null 2>&1; then
            userdel "$WEBAPP_USER" >/dev/null 2>&1 || true
        fi
        if id "$SERVICE_USER" >/dev/null 2>&1; then
            userdel "$SERVICE_USER" >/dev/null 2>&1 \
                || warn "Пользователя $SERVICE_USER удалить не удалось"
        fi
    fi

    printf '\n%s%sБот удалён.%s\n' "$BOLD" "$GREEN" "$RESET"
}

interactive_menu() {
    if [ "$INTERACTIVE" -eq 0 ]; then
        fail "--menu требует терминал. Без него: --yes (установка) или --uninstall."
    fi
    printf '\n%sStackSentry — установка%s\n\n' "$BOLD" "$RESET"
    printf '  1) Установить или обновить бота\n'
    printf '  2) Удалить бота целиком (код, база, пользователь)\n'
    printf '  3) Удалить бота, оставив базу и пользователя\n'
    printf '  h) Справка\n'
    printf '  0) Выход\n\n'
    ask "Выберите пункт [1]: "
    read -r choice <&$READ_FD || choice=""
    case "${choice:-1}" in
        1) return 0 ;;
        2) UNINSTALL=1 ;;
        3) UNINSTALL=1; KEEP_STATE=1 ;;
        h|H|help|--help) usage; exit 0 ;;
        0|q|Q) exit 0 ;;
        *) fail "Не понял выбор: $choice" ;;
    esac
}

if [ "$MENU" -eq 1 ]; then
    interactive_menu
fi

if [ "$UNINSTALL" -eq 1 ]; then
    uninstall
    exit 0
fi

# Шаги: проверка системы, пакеты, пользователь, копирование, venv, конфиг,
# sudo-обёртка, systemd-юнит, запуск, самопроверка, итог — 11 шагов.
# Ветки «Запускаем бота» и «Запуск пропущен» — это один и тот же шаг,
# поэтому счётчик не зависит от --no-start.
TOTAL_STEPS=11

# --------------------------------------------------------------- проверки

step "Проверяем систему"

# Страховка: если репозиторий снова станет заглушкой OWNER (или REPO), загрузка
# из интернета заведомо не сработает — предупреждаем сразу, а не в конце.
if repo_is_placeholder; then
    warn "REPO_OWNER=OWNER — это заглушка: загрузка из интернета не сработает."
    warn "Укажите свой репозиторий: --repo ваш-логин/stacksentry-bot (или REPO_OWNER/REPO_NAME)."
fi

[ "$(id -u)" -eq 0 ] || fail "Запускать от root: sudo bash install.sh"

# --- менеджер служб: systemd, OpenRC или SysV ---
# Бот работает и там, где systemd нет: Alpine/Gentoo (OpenRC) и старые SysVinit.
# Права и ограничения задаёт sudoers-обёртка, а не менеджер служб, поэтому
# функциональность сохраняется; теряется только systemd-изоляция юнита.
if [ -z "$INIT" ]; then
    if [ -d /run/systemd/system ]; then
        INIT=systemd
    elif command -v rc-service >/dev/null 2>&1; then
        INIT=openrc
    elif command -v service >/dev/null 2>&1; then
        INIT=sysv
    else
        fail "Не найден менеджер служб: ни systemd, ни OpenRC, ни SysVinit."
    fi
fi
case "$INIT" in
    systemd)
        command -v systemctl >/dev/null 2>&1 || fail "INIT=systemd, но systemctl не найден"
        ok "Менеджер служб: systemd"
        ;;
    openrc)
        command -v rc-service >/dev/null 2>&1 || fail "INIT=openrc, но rc-service не найден"
        ok "Менеджер служб: OpenRC (Alpine/Gentoo)"
        ;;
    sysv)
        command -v service >/dev/null 2>&1 || fail "INIT=sysv, но service не найден"
        ok "Менеджер служб: SysVinit"
        ;;
    *)
        fail "INIT=$INIT не поддерживается (systemd, openrc, sysv)"
        ;;
esac
SYSTEMD_ACTIVE=0
[ "$INIT" = systemd ] && SYSTEMD_ACTIVE=1
INIT_SCRIPT="/etc/init.d/${SERVICE_NAME}"

command -v sudo >/dev/null 2>&1 \
    || fail "Нужен sudo: через него бот выполняет привилегированные операции."

# --- дистрибутив и пакетный менеджер ---
PKG_INSTALL=""
PKG_UPDATE=""
VENV_PKGS="python3-venv"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091  # /etc/os-release — штатный файл дистрибутива
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_NAME="${PRETTY_NAME:-$OS_ID}"
else
    OS_ID="unknown"
    OS_NAME="неизвестная ОС"
    warn "Не удалось прочитать /etc/os-release"
fi

case "$OS_ID" in
    debian|ubuntu|linuxmint|pop|raspbian|kali|neon|elementary|zorin|devuan)
        PKG_INSTALL="apt-get install -y"
        PKG_UPDATE="apt-get update -qq"
        VENV_PKGS="python3-venv"
        ;;
    fedora)
        PKG_INSTALL="dnf install -y"
        VENV_PKGS="python3"
        ;;
    rhel|centos|rocky|almalinux|ol|oracle|scientific)
        if command -v dnf >/dev/null 2>&1; then
            PKG_INSTALL="dnf install -y"
        else
            PKG_INSTALL="yum install -y"
        fi
        VENV_PKGS="python3"
        ;;
    opensuse*|sles|sled)
        PKG_INSTALL="zypper --non-interactive install"
        PKG_UPDATE="zypper --non-interactive refresh"
        VENV_PKGS="python3"
        ;;
    arch|manjaro|endeavouros|garuda)
        PKG_INSTALL="pacman -S --noconfirm --needed"
        VENV_PKGS="python"
        ;;
    gentoo|funtoo)
        PKG_INSTALL="emerge --noreplace"
        VENV_PKGS="dev-lang/python"
        ;;
    solus)
        PKG_INSTALL="eopkg install -y"
        VENV_PKGS="python3"
        ;;
    alpine)
        PKG_INSTALL="apk add --no-cache"
        PKG_UPDATE="apk update"
        VENV_PKGS="python3 py3-pip py3-virtualenv"
        ;;
    void)
        PKG_INSTALL="xbps-install -Sy"
        VENV_PKGS="python3 python3-pip"
        ;;
    *)
        # Дистрибутив не из списка — выбираем пакетный менеджер по факту наличия.
        for pair in \
            "apt-get:apt-get install -y:python3-venv" \
            "dnf:dnf install -y:python3" \
            "yum:yum install -y:python3" \
            "zypper:zypper --non-interactive install:python3" \
            "pacman:pacman -S --noconfirm --needed:python" \
            "apk:apk add --no-cache:python3 py3-pip py3-virtualenv" \
            "xbps-install:xbps-install -Sy:python3 python3-pip" \
            "pkg:pkg install -y:python311" \
            "emerge:emerge --noreplace:dev-lang/python" \
            "eopkg:eopkg install -y:python3"
        do
            _mgr="${pair%%:*}"
            _rest="${pair#*:}"
            _cmd="${_rest%%:*}"
            _pkgs="${_rest#*:}"
            if command -v "$_mgr" >/dev/null 2>&1; then
                PKG_INSTALL="$_cmd"
                VENV_PKGS="$_pkgs"
                break
            fi
        done
        warn "Дистрибутив $OS_NAME не из проверенных — полагаюсь на пакетный менеджер"
        ;;
esac
ok "ОС: $OS_NAME"

# --- Python 3.10+: подходит любой интерпретатор из PATH ---
MIN_PY="3.10"
PYTHON_BIN=""
# shellcheck disable=SC2086  # ${PYTHON:-} может быть пустым — это нормально
for candidate in ${PYTHON:-} python3.13 python3.12 python3.11 python3.10 python3; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PY//./, }) else 1)" 2>/dev/null; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    fail "Нужен Python ${MIN_PY} или новее. Установите его, например:
      Debian/Ubuntu:  sudo apt-get install python3 python3-venv
      RHEL 9:         sudo dnf install python3.11
      Fedora/Arch:    sudo dnf install python3   |   sudo pacman -S python
    и повторите установку."
fi
ok "Python $("$PYTHON_BIN" -c 'import platform; print(platform.python_version())') → $PYTHON_BIN"

# ---------------------------------------------------------------- исходники

SOURCE_DIR=""
for candidate in "$PWD" "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"; do
    candidate="$(readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate")"
    # При --update нельзя брать исходники из $PREFIX: там лежит старый код,
    # и «обновление» перезаписывало только commit в метаданных.
    if [ "$UPDATE" -eq 1 ] \
        && [ "$candidate" = "$(readlink -f "$PREFIX" 2>/dev/null || printf '%s' "$PREFIX")" ]; then
        continue
    fi
    if [ -f "$candidate/stacksentry/app.py" ]; then
        SOURCE_DIR="$candidate"
        break
    fi
done

if [ -z "$SOURCE_DIR" ]; then
    TMP_DIR="$(mktemp -d)"

    # 1) tar.gz: основной способ — работает без git и за брандмауэрами.
    if command -v curl >/dev/null 2>&1 && [ -n "$TARBALL_URL" ]; then
        while IFS= read -r _candidate; do
            [ -n "$_candidate" ] || continue
            # У codeload.github.com кэшируется архив ветки: сразу после push по
            # refs/heads/* может прилететь прошлый снимок. Параметр-«метка
            # времени» меняет ключ кэша, поэтому версия всегда свежая.
            _fetch_url="$_candidate"
            case "$_fetch_url" in
                *codeload.github.com/*)
                    _fetch_url="${_fetch_url}?t=$(date +%s)"
                    ;;
            esac
            info "Скачиваю исходники: $_candidate"
            _auth_header=""
            _gh_token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
            case "$_fetch_url" in
                https://github.com/*|https://codeload.github.com/*)
                    [ -n "$_gh_token" ] && _auth_header="Authorization: Bearer $_gh_token" ;;
            esac
            if curl_secure "$_auth_header" "$_fetch_url" \
                -fsSL --max-time 120 -o "$TMP_DIR/src.tar.gz" \
                && verify_archive "$TMP_DIR/src.tar.gz" \
                && tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR" 2>/dev/null; then
                SOURCE_DIR="$(find "$TMP_DIR" -maxdepth 2 -name stacksentry \
                    -type d -exec dirname {} \; | head -1)"
                if [ -n "$SOURCE_DIR" ]; then
                    break
                fi
            fi
        done <<EOF
$(source_candidates)
EOF
    fi

    # Если задана контрольная сумма, непроверенный git-клон недопустим:
    # иначе --sha256 обходится одним недоступным архивом.
    if [ -z "$SOURCE_DIR" ] && [ -n "$SOURCE_SHA256" ]; then
        fail "Задан --sha256, но ни один архив не скачался или сумма не совпала.
  Git-fallback отключён: непроверенный код ставить нельзя.
  Проверьте доступ к архиву или снимите --sha256."
    fi

    # 2) git clone: запасной путь, когда тарбол недоступен и сумма не задана.
    if [ -z "$SOURCE_DIR" ] && [ -z "$SOURCE_SHA256" ] \
        && command -v git >/dev/null 2>&1; then
        GIT_URL="${GIT_URL:-$REPO_URL}"
        info "Тарбол не подошёл — пробую git clone $GIT_URL ($REPO_REF)"
        if git clone --depth 1 --branch "$REPO_REF" "$GIT_URL" "$TMP_DIR/repo" \
            >/dev/null 2>&1 && [ -d "$TMP_DIR/repo/stacksentry" ]; then
            SOURCE_DIR="$TMP_DIR/repo"
        fi
    fi

    if [ -z "$SOURCE_DIR" ] || [ ! -d "$SOURCE_DIR" ]; then
        if repo_is_placeholder; then
            fail "Исходники не найдены, а репозиторий — заглушка OWNER.
  Укажите свой репозиторий GitHub, например:
      sudo bash install.sh --repo ваш-логин/stacksentry-bot
  (одной строкой:  sudo bash <(curl -Ls https://raw.githubusercontent.com/ваш-логин/stacksentry-bot/main/install.sh))"
        fi
        fail "Не удалось получить исходники. Варианты:
      • свой репозиторий:  --repo owner/name  [--ref ветка]
      • прямой архив:      --tarball https://host/stacksentry-bot.tar.gz
      • git:               GIT_URL=https://github.com/вас/repo.git
      • или скачайте репозиторий вручную и запустите install.sh из его каталога"
    fi
    ok "Исходники получены во временном каталоге"
fi
ok "Исходники: $SOURCE_DIR"

# Commit установленной версии нужен кнопке «Проверить обновления». В tar.gz
# его нет, поэтому спрашиваем GitHub API (для git-клона достаточно rev-parse).
source_commit() {
    local sha=""
    if command -v git >/dev/null 2>&1; then
        sha="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
    fi
    [ -n "$sha" ] && { printf '%s' "$sha"; return 0; }
    command -v curl >/dev/null 2>&1 || return 0
    case "$REPO_OWNER" in ""|OWNER|your-name) return 0 ;; esac
    local auth_header=""
    _gh_token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    [ -n "$_gh_token" ] && auth_header="Authorization: Bearer $_gh_token"
    curl_secure "$auth_header" \
        "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/commits/${REPO_REF}" \
        -fsSL --max-time 10 -H "Accept: application/vnd.github+json" \
        2>/dev/null | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("sha",""))' \
        2>/dev/null || true
}

INSTALLED_SHA="$(source_commit)"

STACKSENTRY_PRESENT=0
if systemctl is-active --quiet x-ui 2>/dev/null; then
    ok "Панель 3X-UI запущена (юнит x-ui)"
    STACKSENTRY_PRESENT=1
elif systemctl list-unit-files 2>/dev/null | grep -q '^x-ui\.service'; then
    warn "Юнит x-ui есть, но панель сейчас не запущена"
    STACKSENTRY_PRESENT=1
else
    warn "Юнит x-ui не найден — бот запустится, но следить за панелью не сможет"
fi

# --------------------------------------------------------------- пакеты

step "Ставим системные зависимости"

if [ -n "$PKG_UPDATE" ]; then
    info "обновляем индекс пакетов…"
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive $PKG_UPDATE >/dev/null 2>&1 \
        || warn "обновление индекса пакетов не отработало, продолжаем"
fi

MISSING=""
if ! "$PYTHON_BIN" -c 'import venv' >/dev/null 2>&1 \
    || ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
    MISSING="$VENV_PKGS"
fi

if [ -n "$MISSING" ]; then
    if [ -n "$PKG_INSTALL" ]; then
        info "устанавливаем:$MISSING"
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive $PKG_INSTALL $MISSING >/dev/null 2>&1 \
            || fail "Не удалось поставить:$MISSING"
        ok "Установлено:$MISSING"
    else
        warn "Не хватает:$MISSING — поставьте вручную: sudo $PKG_INSTALL$MISSING"
    fi
fi
ok "Системные зависимости на месте"

# --------------------------------------------------------- пользователь

step "Создаём системного пользователя $SERVICE_USER"

# Оболочка «без логина» лежит в разных местах в зависимости от дистрибутива:
# /usr/sbin/nologin (Debian/RHEL/SUSE), /usr/bin/nologin (Arch), /bin/false.
NOLOGIN_SHELL=""
for candidate_shell in /usr/sbin/nologin /sbin/nologin /usr/bin/nologin \
    /bin/nologin /bin/false; do
    if [ -x "$candidate_shell" ]; then
        NOLOGIN_SHELL="$candidate_shell"
        break
    fi
done
NOLOGIN_SHELL="${NOLOGIN_SHELL:-/bin/false}"

if id "$SERVICE_USER" >/dev/null 2>&1; then
    ok "Пользователь $SERVICE_USER уже есть"
elif [ "$SERVICE_USER" = "root" ]; then
    fail "SERVICE_USER=root — бот получил бы полные права. Выберите другого пользователя."
else
    useradd --system --no-create-home --home-dir "$PREFIX" \
        --shell "$NOLOGIN_SHELL" --comment "3X-UI monitor bot" "$SERVICE_USER" \
        || fail "Не удалось создать пользователя $SERVICE_USER"
    ok "Создан системный пользователь $SERVICE_USER"
fi
# Группа может не совпадать с именем пользователя (существующий пользователь
# с другой основной группой): права на .env и unit Group= берём из неё.
SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || printf '%s' "$SERVICE_USER")"

# ------------------------------------------------- миграция старой установки

migrate_legacy_install() {
    # Переносим установки обоих старых имён: uisentry (предыдущий rename) и
    # xui-monitor-bot (исходное). Конфиг/база сохраняются, старые юниты,
    # обёртки, пользователи и каталоги удаляются.
    local profile old_prefix old_state old_user old_service
    for profile in \
        "/opt/uisentry:/var/lib/uisentry:uisentry:uisentry-bot" \
        "/opt/xui-monitor-bot:/var/lib/xui-monitor:xui-monitor:xui-monitor-bot"; do
        IFS=: read -r old_prefix old_state old_user old_service <<<"$profile"
        if [ ! -d "$old_prefix" ] && ! id "$old_user" >/dev/null 2>&1 \
            && [ ! -f "/etc/systemd/system/${old_service}.service" ]; then
            continue
        fi
        if [ "$PREFIX" != "/opt/stacksentry" ] && [ "$PREFIX" != "$old_prefix" ]; then
            warn "Найдена старая установка $old_prefix, но путь $PREFIX нестандартный — миграцию пропускаю"
            continue
        fi
        if [ "$PREFIX" = "$old_prefix" ]; then
            warn "Установка остаётся в старом каталоге $old_prefix; для переезда запустите без --prefix"
        else
            info "Найдена старая установка $old_prefix — переношу конфиг и состояние"
        fi
        if command -v systemctl >/dev/null 2>&1; then
            systemctl disable --now "$old_service" >/dev/null 2>&1 || true
            systemctl disable --now "${old_user}-terminal" >/dev/null 2>&1 || true
            systemctl disable --now "${old_user}-webapp" >/dev/null 2>&1 || true
        fi
        mkdir -p "$PREFIX" "$STATE_DIR"
        if [ -f "$old_prefix/.env" ]; then
            [ -f "$PREFIX/.env" ] || cp -a "$old_prefix/.env" "$PREFIX/.env"
            sed -i "s#^STATE_PATH=.*#STATE_PATH=$STATE_DIR/state.db#; \
                    s#^LOG_PATH=.*#LOG_PATH=$STATE_DIR/bot.log#; \
                    s#^SUDO_WRAPPER=.*#SUDO_WRAPPER=$WRAPPER_PATH#" "$PREFIX/.env" 2>/dev/null || true
        fi
        if [ -f "$old_state/state.db" ] && [ ! -f "$STATE_DIR/state.db" ]; then
            cp -a "$old_state"/state.db* "$STATE_DIR/" 2>/dev/null || true
            cp -a "$old_state"/bot.log* "$STATE_DIR/" 2>/dev/null || true
        fi
        # Секреты терминала/веб-приложения старого имени.
        if [ -d "/etc/$old_user" ] && [ ! -d "/etc/stacksentry" ]; then
            cp -a "/etc/$old_user" "/etc/stacksentry" 2>/dev/null || true
        fi
        chown -R "$SERVICE_USER":"${SERVICE_GROUP:-$SERVICE_USER}" "$STATE_DIR" 2>/dev/null || true
        if [ -f "$PREFIX/.env" ]; then
            chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$PREFIX/.env" 2>/dev/null || true
            chmod 640 "$PREFIX/.env"
        fi
        rm -f "/etc/systemd/system/${old_service}.service" \
            /etc/systemd/system/"${old_user}"-apt-*.service \
            /etc/systemd/system/"${old_user}"-release-upgrade.service \
            /etc/systemd/system/"${old_user}"-terminal.service \
            /etc/systemd/system/"${old_user}"-webapp.service \
            "/etc/sudoers.d/${old_service}" \
            "/etc/sudoers.d/${old_service}.invalid" \
            "/etc/sudoers.d/${old_user}-terminal" \
            /usr/local/bin/"${old_user}"-ctl \
            /usr/local/bin/"${old_user}"-menu \
            "/etc/init.d/${old_service}"
        rm -f /usr/local/bin/xui-ctl /usr/local/bin/xui-menu 2>/dev/null || true
        if command -v systemctl >/dev/null 2>&1; then
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
        if [ "$old_prefix" != "$PREFIX" ] && [ -f "$PREFIX/.env" ]; then
            rm -rf "$old_prefix"
        fi
        if [ "$old_state" != "$STATE_DIR" ]; then
            rm -rf "$old_state"
        fi
        for _old_user in "$old_user" "${old_user}-term" "${old_user}-shell"; do
            if id "$_old_user" >/dev/null 2>&1; then
                userdel "$_old_user" >/dev/null 2>&1 || true
            fi
        done
        if [ -n "$old_user" ]; then
            rm -rf "/etc/${old_user:?}" "/var/lib/${old_user:?}-term" \
                "/var/lib/${old_user:?}-shell" 2>/dev/null || true
        fi
        ok "Старая установка $old_prefix перенесена и удалена"
    done
}

migrate_legacy_install

step "Настраиваем бота (.env)"

ENV_FILE="$PREFIX/.env"

# Мастер спрашивает раньше копирования кода, поэтому каталог установки нужно
# создать прямо здесь: иначе .env писать некуда (на чистой машине /opt пуст).
mkdir -p "$PREFIX" || fail "Не удалось создать каталог $PREFIX"
chmod 755 "$PREFIX"

# ctl читает PREFIX отсюда: self-update должен работать и при --prefix.
if [ -d /etc ]; then
    printf 'PREFIX=%s\n' "$PREFIX" > /etc/stacksentry.conf 2>/dev/null || true
    chmod 0644 /etc/stacksentry.conf 2>/dev/null || true
    chown root:root /etc/stacksentry.conf 2>/dev/null || true
fi

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -1; }

set_env() {
    grep -vE "^$1=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
    # umask установщика — 077: после mv .env стал бы root:root 600 и сервис
    # (User=stacksentry) не смог бы его прочитать при старте.
    chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$ENV_FILE" 2>/dev/null || true
    chmod 640 "$ENV_FILE" 2>/dev/null || true
}

# Обновление не должно молча терять уже установленные компоненты.
if [ "$TERMINAL_EXPLICIT" -eq 0 ] \
    && [ -f /etc/systemd/system/stacksentry-terminal.service ]; then
    TERMINAL=1
fi
if [ "$WEBAPP_EXPLICIT" -eq 0 ] \
    && { [ -f /etc/systemd/system/stacksentry-webapp.service ] \
        || [ "$(env_get WEBAPP_ENABLED)" = "1" ]; }; then
    WEBAPP=1
fi

# Свежая обычная установка: полный набор сразу, без отдельных флагов.
# Явные --no-terminal/--no-webapp/--no-root-terminal/--no-2fa это отключают.
if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ] \
    && [ ! -f "$ENV_FILE" ]; then
    [ "$TERMINAL_EXPLICIT" -eq 0 ] && TERMINAL=1
    [ "$WEBAPP_EXPLICIT" -eq 0 ] && WEBAPP=1
    [ "$TERMINAL_EXPLICIT" -eq 0 ] && [ "$ROOT_TERMINAL_EXPLICIT" -eq 0 ] \
        && TERMINAL_ALLOW_ROOT=1
fi

# Уже установленный бот: предложить недостающие компоненты, а не молча
# оставить Mini App/терминал выключенными после обновления.
if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
    if [ "$WEBAPP_EXPLICIT" -eq 0 ] && [ "$WEBAPP" -eq 0 ]; then
        ask "Установить Mini App (панель в Telegram)? [Y/n] "
        read -r _component_answer <&$READ_FD || _component_answer=""
        case "$_component_answer" in
            ""|y|Y|yes|да|д) WEBAPP=1 ;;
        esac
    fi
    if [ "$TERMINAL_EXPLICIT" -eq 0 ] && [ "$TERMINAL" -eq 0 ]; then
        ask "Установить веб-терминал (TOTP + root)? [Y/n] "
        read -r _component_answer <&$READ_FD || _component_answer=""
        case "$_component_answer" in
            ""|y|Y|yes|да|д)
                TERMINAL=1
                [ "$ROOT_TERMINAL_EXPLICIT" -eq 0 ] && TERMINAL_ALLOW_ROOT=1
                ;;
        esac
    fi
fi

question() {
    # question ПЕРЕМЕННАЯ "подсказка" "значение по умолчанию"
    local __var="$1" __prompt="$2" __default="$3" __answer="" __secret=0
    case "$__var" in
        *TOKEN* | *PASSWORD* | *SECRET*) __secret=1 ;;
    esac
    if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        ask "${BOLD}${__prompt}${RESET}"
        if [ "$__secret" -eq 1 ]; then
            # Не показываем текущий секрет и не оставляем его в истории ввода.
            printf ' %s(ввод скрыт, Enter — оставить прежнее)%s ' "$DIM" "$RESET"
            read -rs __answer <&$READ_FD || __answer=""
            printf '\n'
        else
            if [ -n "$__default" ]; then
                printf ' %s[%s]%s ' "$DIM" "$__default" "$RESET"
            else
                printf ' '
            fi
            read -r __answer <&$READ_FD || __answer=""
        fi
    fi
    printf -v "$__var" '%s' "${__answer:-$__default}"
}

panel_settings() {
    # Печатает порт и webBasePath панели в формате "порт base_path".
    command -v x-ui >/dev/null 2>&1 || { printf '2053 \n'; return 0; }
    x-ui settings 2>/dev/null | "$PYTHON_BIN" -c '
import json, re, sys

raw = sys.stdin.read()
data = {}
try:
    data = json.loads(raw)
except ValueError:
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().strip("\"'\''")
        value = value.strip().strip("\"'\'',")
        if key:
            data[key] = value

port = data.get("webPort") or data.get("webListen") or ""
match = re.search(r"[0-9]+", str(port))
port = match.group(0) if match else "2053"
base = str(data.get("webBasePath") or "").strip("/")
print(port, base)
'
}

panel_api_code() {
    # HTTP-код API панели без токена. В 3X-UI правильный путь отвечает 401
    # (нужен токен), неверный — 404. Так webBasePath проверяется до токена.
    curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
        -H 'X-Requested-With: XMLHttpRequest' \
        "$1/panel/api/server/status" 2>/dev/null || echo 000
}

resolve_panel_base() {
    # $1 — адрес панели без пути, $2 — предполагаемый webBasePath.
    # Печатает подтверждённый webBasePath (пусто, если путь не нужен).
    # В 3X-UI адрес в браузере всегда оканчивается на /panel — это не
    # webBasePath, и «/panel» в поле настроек даёт лишний сегмент (404).
    local url="$1" guess="$2" code
    if [ -n "$guess" ]; then
        code="$(panel_api_code "$(collapse_slashes "$url/$guess")")"
        case "$code" in 200|301|302|401|403) printf '%s\n' "$guess"; return 0 ;; esac
    fi
    code="$(panel_api_code "$url")"
    case "$code" in 200|301|302|401|403) printf '\n'; return 0 ;; esac
    printf '%s\n' "$guess"
}

detect_panel_token() {
    # x-ui сам создаёт токен и печатает его. Новое имя токена заменяет старое,
    # поэтому вызываем функцию только осознанно.
    command -v x-ui >/dev/null 2>&1 || return 1
    x-ui setting -getApiToken true -tokenName monitor-bot -tokenScope node-sync 2>/dev/null \
        | "$PYTHON_BIN" -c '
import re, sys

text = sys.stdin.read()
# Сначала значение рядом со словом token («Token: <значение>»), иначе — любое
# длинное слово, кроме названий областей доступа: x-ui печатает рядом список
# scope (например «update-all-geofiles»), и раньше в .env попадало именно
# название области, а не токен.
labelled = re.search(r"(?im)^[^:\n]*token[^:\n]*[:=]\s*([A-Za-z0-9_-]{16,})\s*$", text)
if labelled:
    print(labelled.group(1))
    raise SystemExit(0)

deny = ("successful", "tokenname", "tokenscope", "geofile", "scope", "sync", "admin", "monitor")
found = [
    item
    for item in re.findall(r"[A-Za-z0-9_-]{16,}", text)
    if not any(bad in item.lower() for bad in deny)
]
print(found[0] if found else "")
'
}

# .env может остаться после прерванного запуска: тогда в нём заглушки или пустые
# значения. В этом случае не требуем править файл руками, а дозаполняем мастером
# (существующие значения подставляются как значения по умолчанию).
CONFIG_COMPLETE=0
if [ -f "$ENV_FILE" ] && [ "$KEEP_CONFIG" -eq 1 ]; then
    CONFIG_COMPLETE=1
    for _key in TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_IDS; do
        _value="$(env_get "$_key")"
        case "$_value" in
            "" | *ExampleTokenReplaceMe*) CONFIG_COMPLETE=0 ;;
        esac
    done
    _cfg_panel_type="$(env_get PANEL_TYPE)"
    [ -n "$_cfg_panel_type" ] || _cfg_panel_type="3x-ui"
    if [ -n "$(env_get PANEL_URL)" ] || [ -n "$(env_get PANEL_API_TOKEN)" ]; then
        case "$_cfg_panel_type" in
            marzban|marzneshin|pasarguard)
                for _key in PANEL_URL PANEL_USERNAME PANEL_PASSWORD; do
                    _value="$(env_get "$_key")"
                    [ -n "$_value" ] || CONFIG_COMPLETE=0
                done
                ;;
            outline)
                [ -n "$(env_get PANEL_URL)" ] || CONFIG_COMPLETE=0
                ;;
            *)
                for _key in PANEL_URL PANEL_API_TOKEN; do
                    _value="$(env_get "$_key")"
                    [ -n "$_value" ] || CONFIG_COMPLETE=0
                done
                ;;
        esac
    fi
    # --panel на monitor-only .env должен снова открыть мастер.
    if [ "$PANEL_MODE" = "1" ] && [ -z "$(env_get PANEL_URL)" ]; then
        CONFIG_COMPLETE=0
    fi
fi

if [ "$CONFIG_COMPLETE" -eq 1 ]; then
    ok "Файл .env уже есть — сохраняю ваши токены"
    info "Изменить: sudo stacksentry-menu (или nano $ENV_FILE)"
else
    if [ -f "$ENV_FILE" ] && [ "$KEEP_CONFIG" -eq 1 ]; then
        warn "В $ENV_FILE не всё заполнено — дозаполняю через мастер"
    fi
    [ -f "$ENV_FILE" ] || { cp "$SOURCE_DIR/.env.example" "$ENV_FILE" 2>/dev/null || touch "$ENV_FILE"; }

    read -r DETECTED_PORT DETECTED_BASE <<<"$(panel_settings)"
    # Панель и её тип определяем по службам/путям/процессам, а не «только 3X-UI».
    DETECTED_PANEL_TYPE="$(PYTHONPATH="$SOURCE_DIR" "$PYTHON_BIN" \
        -m stacksentry.inventory --panel-type 2>/dev/null || true)"
    DETECTED_PANEL_PORT="$(PYTHONPATH="$SOURCE_DIR" "$PYTHON_BIN" \
        -m stacksentry.inventory --panel-port 2>/dev/null || true)"
    PANEL_TYPE_VALUE="$(env_get PANEL_TYPE)"
    [ -n "$PANEL_TYPE_VALUE" ] || PANEL_TYPE_VALUE="${DETECTED_PANEL_TYPE:-3x-ui}"
    if [ -n "$DETECTED_PANEL_TYPE" ]; then
        info "Обнаружена панель: $DETECTED_PANEL_TYPE${DETECTED_PANEL_PORT:+ (порт $DETECTED_PANEL_PORT)}"
    fi
    if [ -n "$DETECTED_PANEL_TYPE" ] && [ "$DETECTED_PANEL_TYPE" != "$PANEL_TYPE_VALUE" ] \
        && [ -z "$(env_get PANEL_TYPE)" ]; then
        warn "Тип панели взят из автоопределения: $PANEL_TYPE_VALUE"
    fi
    case "$PANEL_TYPE_VALUE" in
        s-ui) DEFAULT_PANEL_PORT="${DETECTED_PANEL_PORT:-2095}" ;;
        marzban|marzneshin|pasarguard) DEFAULT_PANEL_PORT="${DETECTED_PANEL_PORT:-8000}" ;;
        hiddify|outline) DEFAULT_PANEL_PORT="${DETECTED_PANEL_PORT:-443}" ;;
        remnawave) DEFAULT_PANEL_PORT="${DETECTED_PANEL_PORT:-3000}" ;;
        *) DEFAULT_PANEL_PORT="${DETECTED_PORT:-2053}" ;;
    esac
    HOST="127.0.0.1"
    IP_LIST="$(detect_ips)"
    FIRST_IP="${IP_LIST%% *}"
    if [ -n "$FIRST_IP" ] && [ "$FIRST_IP" != "127.0.0.1" ]; then
        HOST="$FIRST_IP"
    fi
    if [ -n "$IP_LIST" ]; then
        info "IP-адреса этого сервера: $IP_LIST"
        info "Можно вписать любой из них — в ответе укажите адрес, который слышит ваш телефон/VPN."
    fi
    SUGGESTED_URL="${PANEL_URL:-http://${HOST}:${DEFAULT_PANEL_PORT}}"

    question TG_TOKEN "Токен Telegram-бота от @BotFather" \
        "$(printf '%s' "${TELEGRAM_BOT_TOKEN:-$(env_get TELEGRAM_BOT_TOKEN)}")"
    [ -n "$TG_TOKEN" ] || fail "Без токена бота запускать бессмысленно. Создайте бота у @BotFather и вставьте токен."
    case "$TG_TOKEN" in
        *:*) ;;
        *) fail "Токен @BotFather выглядит как 123456:AA... — проверьте вставку" ;;
    esac

    # Первичная сверка с Telegram: ошибка токена видна сразу, а не после
    # всей установки. При отсутствии сети просто предупреждаем.
    TG_API_VALUE="${TELEGRAM_API_URL:-https://api.telegram.org}"
    TG_PROBE="$(curl_secure "" "${TG_API_VALUE%/}/bot${TG_TOKEN}/getMe" \
        -sS --max-time 15 2>/dev/null || true)"
    if printf '%s' "$TG_PROBE" | grep -q '"ok":true'; then
        TG_USERNAME="$(printf '%s' "$TG_PROBE" | sed -E 's/.*"username":"([^"]+)".*/\1/')"
        ok "Telegram принял токен: бот @$TG_USERNAME"
    elif printf '%s' "$TG_PROBE" | grep -q '"ok":false'; then
        case "$TG_TOKEN" in
            *ExampleTokenReplaceMe*)
                fail "TELEGRAM_BOT_TOKEN — заглушка. Откройте @BotFather -> /newbot и вставьте настоящий токен." ;;
            *)
                fail "Telegram отклонил токен (${TG_API_VALUE}). Скопируйте его заново из @BotFather." ;;
        esac
    else
        warn "Telegram не отвечает — проверим токен в самопроверке в конце"
    fi

    question ADMIN_IDS "Ваш Telegram ID (у @userinfobot), можно несколько через запятую" \
        "$(printf '%s' "${TELEGRAM_ADMIN_IDS:-$(env_get TELEGRAM_ADMIN_IDS)}")"
    [ -n "$ADMIN_IDS" ] || fail "Нужен хотя бы один Telegram ID администратора"
    printf '%s' "$ADMIN_IDS" | grep -qE '^[0-9]+(,[0-9]+)*$' \
        || fail "ADMIN_IDS должны быть числами через запятую, получено: $ADMIN_IDS"

    # --- 2FA: Mini App и опасные действия ---
    configure_2fa

    # --- API панели: автоопределение типа (3X-UI/Marzban/Remnawave/S-UI/Hiddify) ---
    if [ "$PANEL_MODE" = "auto" ]; then
        if [ -n "$(env_get PANEL_URL)" ]; then
            PANEL_MODE=1
        else
            PANEL_MODE="$(PYTHONPATH="$SOURCE_DIR" "$PYTHON_BIN" -m stacksentry.inventory --panel 2>/dev/null || echo 0)"
        fi
    fi
    if [ "$PANEL_MODE" = "0" ] && [ "$PANEL_MODE_EXPLICIT" -eq 0 ] \
        && [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        ask "Поддерживаемая API-панель не найдена. Настроить вручную? [y/N] "
        read -r _panel_answer <&$READ_FD || _panel_answer=""
        case "$_panel_answer" in
            [yYдД]*) PANEL_MODE=1 ;;
            *) PANEL_MODE=0 ;;
        esac
    fi

    PANEL_USERNAME_VALUE="$(env_get PANEL_USERNAME)"
    PANEL_PASSWORD_VALUE="$(env_get PANEL_PASSWORD)"
    if [ "$PANEL_MODE" = "1" ]; then
    if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        question PANEL_TYPE_VALUE "Тип панели (3x-ui/marzban/marzneshin/pasarguard/remnawave/s-ui/hiddify/outline)" "$PANEL_TYPE_VALUE"
    fi
    PANEL_TYPE_VALUE="$(printf '%s' "$PANEL_TYPE_VALUE" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    case "$PANEL_TYPE_VALUE" in
        3x-ui|marzban|marzneshin|pasarguard|remnawave|s-ui|hiddify|outline) ;;
        *) fail "PANEL_TYPE: поддерживаются 3x-ui, marzban, marzneshin, pasarguard, remnawave, s-ui, hiddify и outline" ;;
    esac
    if [ "$PANEL_TYPE_VALUE" = "3x-ui" ]; then
    PANEL_USERNAME_VALUE=""
    PANEL_PASSWORD_VALUE=""
    _existing_panel_url="$(env_get PANEL_URL)"
    [ -n "$_existing_panel_url" ] && SUGGESTED_URL="$_existing_panel_url"
    question PANEL_URL_VALUE "Адрес панели 3X-UI (хост и порт)" "$SUGGESTED_URL"
    PANEL_URL_VALUE="$(normalize_url "$PANEL_URL_VALUE")"
    # Адрес можно вставлять прямо из браузера, вместе с путём — отделяем путь.
    read -r PANEL_URL_VALUE URL_PATH_SUGGEST <<<"$(split_panel_address "$PANEL_URL_VALUE")"
    if [ -n "$URL_PATH_SUGGEST" ]; then
        info "В адресе был путь — переношу его в webBasePath: /$URL_PATH_SUGGEST"
    fi
    case "$PANEL_URL_VALUE" in
        http://*|https://*) ;;
        *) PANEL_URL_VALUE="$(normalize_url "$SUGGESTED_URL")"
           warn "Некорректный адрес, беру автоопределённый: $PANEL_URL_VALUE" ;;
    esac

    question PANEL_BASE "webBasePath — поле «URL панели» в настройках 3X-UI (обычно /, тогда просто Enter). Это НЕ путь /panel из адресной строки" \
        "${PANEL_BASE_PATH:-${URL_PATH_SUGGEST:-${DETECTED_BASE}}}"
    PANEL_BASE="$(normalize_path "$PANEL_BASE")"
    # Если путь был в адресе, а поле оставили пустым — берём его оттуда.
    if [ -z "$PANEL_BASE" ] && [ -n "$URL_PATH_SUGGEST" ]; then
        PANEL_BASE="$URL_PATH_SUGGEST"
    fi
    case "$PANEL_BASE" in
        *://*)
            # Частая ошибка: в webBasePath вставили полный адрес панели.
            warn "В webBasePath вставлен адрес — использую его как адрес панели, путь очищаю"
            PANEL_URL_VALUE="$(normalize_url "$PANEL_BASE")"
            PANEL_BASE=""
            ;;
    esac

    # Ровно один слеш между адресом и путём: иначе панель отвечает 404.
    PROBE_URL="$PANEL_URL_VALUE"
    [ -n "$PANEL_BASE" ] && PROBE_URL="$PANEL_URL_VALUE/$PANEL_BASE"
    PROBE_URL="$(collapse_slashes "$PROBE_URL")"
    PANEL_URL_VALUE="$(collapse_slashes "$PANEL_URL_VALUE")"

    # Проверяем webBasePath и при необходимости исправляем, не дожидаясь токена.
    _entered_base="$PANEL_BASE"
    PANEL_BASE="$(resolve_panel_base "$PANEL_URL_VALUE" "$PANEL_BASE")"
    if [ "$PANEL_BASE" != "$_entered_base" ]; then
        warn "webBasePath «${_entered_base:-/}» не подтвердился — беру «${PANEL_BASE:-/}»"
        info "В 3X-UI интерфейс всегда на .../panel, а webBasePath задаётся отдельным"
        info "полем «URL панели» (обычно /). Путь /panel в webBasePath даёт лишний сегмент."
    fi
    PROBE_URL="$PANEL_URL_VALUE"
    [ -n "$PANEL_BASE" ] && PROBE_URL="$PANEL_URL_VALUE/$PANEL_BASE"
    PROBE_URL="$(collapse_slashes "$PROBE_URL")"
    if curl -sS --max-time 5 -o /dev/null "$PROBE_URL/" 2>/dev/null; then
        ok "Панель отвечает: $PROBE_URL"
    else
        warn "Панель не ответила на $PROBE_URL/ — если адрес верный, это заметит самопроверка"
    fi

    info "3X-UI v3: «Настройки панели → Учётная запись → API-токен» (scope node-sync"
    info "или admin) — значение показывается один раз, копируйте сразу."
    info "3X-UI 2.x: «Настройки → API Tokens» либо sudo x-ui setting -getApiToken true"
    info "-tokenName monitor-bot -tokenScope node-sync (в v3 эта команда больше не создаёт токены)."
    question PANEL_TOKEN "API-токен панели" "${PANEL_API_TOKEN:-$(env_get PANEL_API_TOKEN)}"
    if [ -z "$PANEL_TOKEN" ]; then
        if [ "$STACKSENTRY_PRESENT" -eq 1 ] && { [ "$ASSUME_YES" -eq 0 ] || [ -n "${ALLOW_TOKEN_GEN:-}" ]; }; then
            warn "Выпускаю токен monitor-bot (scope node-sync) в панели."
            warn "Внимание: если такой токен уже был, старый перестанет работать."
            PANEL_TOKEN="$(detect_panel_token || true)"
        fi
        [ -n "$PANEL_TOKEN" ] || fail "Нужен API-токен панели:
      3X-UI v3: «Настройки панели → Учётная запись → API-токен» → создать со scope
      node-sync (или admin) и скопировать значение сразу — панель показывает его
      только один раз. То же доступно по API: POST /panel/api/setting/apiTokens/create
      (name, scope, expiresAt; нужен уже существующий admin-токен).
      3X-UI 2.x: «Настройки → API Tokens», либо из терминала:
      sudo x-ui setting -getApiToken true -tokenName monitor-bot -tokenScope node-sync
   Полученное значение впишите в PANEL_API_TOKEN."
    fi

    # Сразу проверяем токен: без этого в .env попадал мусор (например, название
    # области доступа), и бот потом ругался «токен не принят».
    if [ -n "$PANEL_TOKEN" ] && [ -n "$PROBE_URL" ]; then
        _tok_tmp="$(mktemp)"
        _token_http="$(curl_secure "Authorization: Bearer $PANEL_TOKEN" \
            "${PROBE_URL}/panel/api/server/status" \
            -sS --max-time 5 -o "$_tok_tmp" -w '%{http_code}' 2>/dev/null || echo 000)"
        _token_reply="$(cat "$_tok_tmp" 2>/dev/null || true)"
        rm -f "$_tok_tmp"
        if printf '%s' "$_token_reply" | grep -q '"success":true'; then
            ok "Токен панели принят"
        elif printf '%s' "$_token_reply" | grep -q '"success":false' || [ "$_token_http" = "401" ] || [ "$_token_http" = "403" ]; then
            if [ "$ASSUME_YES" -eq 1 ]; then
                fail "Панель отклонила API-токен (HTTP ${_token_http}).
      3X-UI v3: «Настройки панели → Учётная запись → API-токен» — создайте токен со
      scope node-sync (или admin) и вставьте значение; посмотреть его позже панель
      не даст, копируйте сразу.
      3X-UI 2.x: «Настройки → API Tokens» либо
      sudo x-ui setting -getApiToken true -tokenName monitor-bot -tokenScope node-sync"
            fi
            warn "Панель отклонила токен (HTTP ${_token_http}) — проверьте значение и его область (нужен node-sync или admin)."
            info "3X-UI v3: Настройки панели → Учётная запись → API-токен (создать, скопировать сразу);"
            info "то же по API: POST /panel/api/setting/apiTokens/create с scope node-sync (нужен admin-токен)."
            info "3X-UI 2.x: sudo x-ui setting -getApiToken true -tokenName monitor-bot -tokenScope node-sync"
            info "В PANEL_API_TOKEN вставляется значение токена, а не название области — например update-all-geofiles."
        elif [ "$_token_http" = "404" ]; then
            warn "Панель ответила 404 — похоже, адрес API неверен (PANEL_URL/webBasePath)"
        else
            warn "Проверить токен не удалось (HTTP ${_token_http}, панель не ответила) — это заметит самопроверка"
        fi
    fi

    else
        # Остальные панели: адрес (у S-UI/Hiddify/Outline — вместе с путём),
        # затем токен либо логин/пароль в зависимости от типа.
        PANEL_USERNAME_VALUE=""
        PANEL_PASSWORD_VALUE=""
        case "$PANEL_TYPE_VALUE" in
            s-ui)
                info "S-UI: адрес вместе с web path панели, например http://host:2095/app"
                question PANEL_URL_VALUE "Адрес S-UI (с путём панели)" "$SUGGESTED_URL"
                ;;
            hiddify)
                info "Hiddify: адрес вместе с admin proxy path, например https://host/AbCdEf"
                question PANEL_URL_VALUE "Адрес Hiddify (с admin proxy path)" "$SUGGESTED_URL"
                ;;
            outline)
                info "Outline: адрес вместе с секретным SB_API_PREFIX, например https://host:port/SecretPath"
                question PANEL_URL_VALUE "Адрес Outline (с секретным путём API)" "$SUGGESTED_URL"
                ;;
            *)
                question PANEL_URL_VALUE "Адрес панели $PANEL_TYPE_VALUE" "$SUGGESTED_URL"
                ;;
        esac
        PANEL_URL_VALUE="$(normalize_url "$PANEL_URL_VALUE")"
        case "$PANEL_URL_VALUE" in
            http://*|https://*) ;;
            *) PANEL_URL_VALUE="$(normalize_url "$SUGGESTED_URL")"
               warn "Некорректный адрес, беру автоопределённый: $PANEL_URL_VALUE" ;;
        esac
        PANEL_URL_VALUE="$(collapse_slashes "$PANEL_URL_VALUE")"
        PANEL_BASE=""
        PROBE_URL="$PANEL_URL_VALUE"
        if curl -sS --max-time 5 -o /dev/null "$PROBE_URL/" 2>/dev/null; then
            ok "Панель отвечает: $PROBE_URL"
        else
            warn "Панель не ответила на $PROBE_URL/ — если адрес верный, это заметит самопроверка"
        fi
        case "$PANEL_TYPE_VALUE" in
            marzban|marzneshin|pasarguard)
                case "$PANEL_TYPE_VALUE" in
                    marzneshin) _login_path="/api/admins/token"; _panel_title="Marzneshin" ;;
                    pasarguard) _login_path="/api/admin/token"; _panel_title="PasarGuard" ;;
                    *) _login_path="/api/admin/token"; _panel_title="Marzban" ;;
                esac
                question PANEL_USERNAME_VALUE "Логин администратора $_panel_title" "${PANEL_USERNAME_VALUE:-admin}"
                [ -n "$PANEL_USERNAME_VALUE" ] || fail "Нужен логин администратора $_panel_title"
                question PANEL_PASSWORD_VALUE "Пароль администратора $_panel_title" "$PANEL_PASSWORD_VALUE"
                [ -n "$PANEL_PASSWORD_VALUE" ] || fail "Нужен пароль администратора $_panel_title"
                _login_tmp="$(mktemp)"
                _login_code="$(curl_secure_form \
                    "$PANEL_URL_VALUE$_login_path" \
                    "$PANEL_USERNAME_VALUE" "$PANEL_PASSWORD_VALUE" \
                    -sS --max-time 10 -o "$_login_tmp" -w '%{http_code}' \
                    2>/dev/null || echo 000)"
                _login_reply="$(cat "$_login_tmp" 2>/dev/null || true)"
                rm -f "$_login_tmp"
                if printf '%s' "$_login_reply" | grep -q '"access_token"'; then
                    ok "$_panel_title принял логин и пароль"
                elif [ "$_login_code" = "401" ] || [ "$_login_code" = "403" ]; then
                    warn "$_panel_title отклонил логин/пароль (HTTP $_login_code) — проверьте учётную запись"
                else
                    warn "Проверить логин $_panel_title не удалось (HTTP $_login_code) — это заметит самопроверка"
                fi
                PANEL_TOKEN=""
                ;;
            outline)
                # У Outline нет отдельного токена: доступ защищён секретным путём.
                PANEL_TOKEN=""
                _probe_code="$(curl_secure "" "$PANEL_URL_VALUE/server" \
                    -sS --max-time 5 -o /dev/null -w '%{http_code}' \
                    2>/dev/null || echo 000)"
                case "$_probe_code" in
                    200) ok "Outline API ответил — секретный путь верный" ;;
                    401|403|404) warn "Outline ответил HTTP $_probe_code — проверьте SB_API_PREFIX в адресе" ;;
                    *) warn "Проверить Outline не удалось (HTTP $_probe_code) — это заметит самопроверка" ;;
                esac
                ;;
            *)
                case "$PANEL_TYPE_VALUE" in
                    s-ui)
                        info "S-UI: Admin → API Token; токен передаётся заголовком Token"
                        ;;
                    hiddify)
                        info "Hiddify: Settings → API; ключ — UUID администратора (Hiddify-API-Key)"
                        ;;
                    *)
                        info "Remnawave: API-токен создаётся в настройках панели"
                        ;;
                esac
                question PANEL_TOKEN "API-токен панели" "${PANEL_API_TOKEN:-$(env_get PANEL_API_TOKEN)}"
                [ -n "$PANEL_TOKEN" ] || fail "Нужен API-токен панели $PANEL_TYPE_VALUE"
                _probe_tmp="$(mktemp)"
                case "$PANEL_TYPE_VALUE" in
                    s-ui)
                        _probe_code="$(curl_secure "Token: $PANEL_TOKEN" \
                            "$PANEL_URL_VALUE/apiv2/status?r=sbd" \
                            -sS --max-time 5 -o "$_probe_tmp" -w '%{http_code}' \
                            2>/dev/null || echo 000)"
                        if grep -q '"success":true' "$_probe_tmp" 2>/dev/null; then
                            ok "S-UI приняла токен"
                        else
                            warn "S-UI не подтвердила токен (HTTP $_probe_code) — проверьте значение и путь панели"
                        fi
                        ;;
                    hiddify)
                        _probe_code="$(curl_secure "Hiddify-API-Key: $PANEL_TOKEN" \
                            "$PANEL_URL_VALUE/api/v2/panel/info/" \
                            -sS --max-time 5 -o /dev/null -w '%{http_code}' \
                            2>/dev/null || echo 000)"
                        case "$_probe_code" in
                            200) ok "Hiddify принял API-ключ" ;;
                            401|403) warn "Hiddify отклонил ключ (HTTP $_probe_code) — проверьте admin UUID и путь" ;;
                            *) warn "Проверить Hiddify не удалось (HTTP $_probe_code) — это заметит самопроверка" ;;
                        esac
                        ;;
                    remnawave)
                        _probe_code="$(curl_secure "Authorization: Bearer $PANEL_TOKEN" \
                            "$PANEL_URL_VALUE/api/users?size=1" \
                            -sS --max-time 5 -o /dev/null -w '%{http_code}' \
                            2>/dev/null || echo 000)"
                        case "$_probe_code" in
                            200) ok "Remnawave принял токен" ;;
                            401|403) warn "Remnawave отклонил токен (HTTP $_probe_code)" ;;
                            *) warn "Проверить Remnawave не удалось (HTTP $_probe_code) — это заметит самопроверка" ;;
                        esac
                        ;;
                esac
                rm -f "$_probe_tmp"
                ;;
        esac
    fi

    else
        PANEL_URL_VALUE=""
        PANEL_BASE=""
        PANEL_TOKEN=""
        PANEL_USERNAME_VALUE=""
        PANEL_PASSWORD_VALUE=""
        PROBE_URL=""
        info "API панели не настроен — режим «только мониторинг служб»."
        info "Позже: sudo stacksentry-menu → тип, адрес и токен панели."
    fi

    # Автоопределение панелей и VPN-стека (stdlib, работает до venv).
    DETECTED_SERVICES="$(PYTHONPATH="$SOURCE_DIR" "$PYTHON_BIN" -m stacksentry.inventory --services 2>/dev/null || true)"
    if [ -n "$DETECTED_SERVICES" ]; then
        info "Найдены службы: $DETECTED_SERVICES"
    else
        warn "Службы не найдены — можно оставить поле пустым (monitor-only)."
    fi
    _services_default="$(env_get SERVICES)"
    [ -n "$_services_default" ] || _services_default="$DETECTED_SERVICES"
    info "Сервисы можно перечислить через запятую; служебные имена — из вашего"
    info "менеджера служб (systemd/OpenRC/SysV). Enter — оставить найденные."
    question SERVICES_LIST "Сервисы под наблюдением" "$_services_default"

    info "Чат для алертов — необязательное поле: Enter — алерты придут в личку"
    info "всем ID из TELEGRAM_ADMIN_IDS."
    info "Нужен общий чат? Впишите его ID (число, у групп оно отрицательное,"
    info "например -1001234567890). Узнать: добавьте бота в группу, отправьте там"
    info "сообщение и посмотрите ID у @userinfobot или @getidsbot."
    question START_CHAT "Чат для алертов (Enter — админам в личку)" "$(env_get ALERT_CHAT_ID)"

    if [ "$PANEL_MODE" = "1" ]; then
        case "$PANEL_TYPE_VALUE" in
            marzban|marzneshin|pasarguard)
                _panel_auth="логин $PANEL_USERNAME_VALUE, пароль ***"
                ;;
            outline)
                _panel_auth="секретный путь API"
                ;;
            *)
                _panel_auth="токен ***${PANEL_TOKEN: -4}"
                ;;
        esac
        info "Итого: бот ${TG_USERNAME:+@$TG_USERNAME }<$ADMIN_IDS>, панель $PANEL_TYPE_VALUE $PANEL_URL_VALUE, сервисы $SERVICES_LIST, $_panel_auth"
    else
        info "Итого: бот ${TG_USERNAME:+@$TG_USERNAME }<$ADMIN_IDS>, панель не настроена (monitor-only), сервисы $SERVICES_LIST"
    fi

    set_env TELEGRAM_BOT_TOKEN "$TG_TOKEN"
    set_env TELEGRAM_ADMIN_IDS "$ADMIN_IDS"
    set_env PANEL_TYPE "$PANEL_TYPE_VALUE"
    set_env PANEL_URL "$PANEL_URL_VALUE"
    set_env PANEL_BASE_PATH "$PANEL_BASE"
    set_env PANEL_API_TOKEN "$PANEL_TOKEN"
    set_env PANEL_USERNAME "$PANEL_USERNAME_VALUE"
    set_env PANEL_PASSWORD "$PANEL_PASSWORD_VALUE"
    set_env SERVICES "$SERVICES_LIST"
    set_env ALERT_CHAT_ID "$START_CHAT"
    set_env STATE_PATH "$STATE_DIR/state.db"
    set_env LOG_PATH "$STATE_DIR/bot.log"
    set_env SUDO_WRAPPER "$WRAPPER_PATH"
    if [ -n "${TELEGRAM_API_URL:-}" ]; then
        set_env TELEGRAM_API_URL "$TELEGRAM_API_URL"
    fi
    ok "Конфигурация записана в $ENV_FILE"
fi

# Флаги и обязательная проверка 2FA работают и на уже готовом .env
# (обычный update): если пункт не настраивался ранее — спросим.
if [ "$CONFIG_COMPLETE" -eq 1 ]; then
    configure_2fa
    if [ "$PANEL_MODE" = "0" ]; then
        set_env PANEL_URL ""
        set_env PANEL_API_TOKEN ""
        set_env PANEL_BASE_PATH ""
        set_env PANEL_USERNAME ""
        set_env PANEL_PASSWORD ""
        info "API панели отключён (--no-panel): monitor-only."
    fi
fi

# .env содержит оба токена. Владелец — root, сервису доступно только чтение
# (systemd отдаёт переменные сам, а бот не может переписать свой конфиг и
# пережить таким образом компрометацию).
chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$ENV_FILE"
chmod 640 "$ENV_FILE"

for key in TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_IDS; do
    [ -n "$(env_get "$key")" ] || fail "В $ENV_FILE пусто $key — дозаполните: sudo stacksentry-menu (или повторите установку)"
done
_final_panel_type="$(env_get PANEL_TYPE)"
[ -n "$_final_panel_type" ] || _final_panel_type="3x-ui"
if [ -n "$(env_get PANEL_URL)" ] || [ -n "$(env_get PANEL_API_TOKEN)" ]; then
    case "$_final_panel_type" in
        marzban|marzneshin|pasarguard)
            for key in PANEL_URL PANEL_USERNAME PANEL_PASSWORD; do
                [ -n "$(env_get "$key")" ] || fail "В $ENV_FILE пусто $key — для $_final_panel_type нужны адрес, логин и пароль"
            done
            ;;
        outline)
            [ -n "$(env_get PANEL_URL)" ] || fail "В $ENV_FILE пуст PANEL_URL — для Outline нужен адрес с SB_API_PREFIX"
            ;;
        *)
            for key in PANEL_URL PANEL_API_TOKEN; do
                [ -n "$(env_get "$key")" ] || fail "В $ENV_FILE пусто $key — задайте оба или очистите оба (monitor-only)"
            done
            ;;
    esac
fi

# Заглушка из .env.example: с ней бот стартует, но Telegram отвечает 401.
case "$(env_get TELEGRAM_BOT_TOKEN)" in
    *ExampleTokenReplaceMe*)
        fail "TELEGRAM_BOT_TOKEN в $ENV_FILE — заглушка из .env.example.
  Возьмите настоящий токен: откройте @BotFather -> /newbot и впишите
  выданное значение вида 123456789:AA... в TELEGRAM_BOT_TOKEN."
        ;;
esac
case "$(env_get TELEGRAM_BOT_TOKEN)" in
    *:*) ;;
    *) fail "TELEGRAM_BOT_TOKEN должен содержать «<bot_id>:<секрет>» — так выдаёт @BotFather" ;;
esac


# ------------------------------------------------------------- файлы бота

step "Копируем бота в $PREFIX"

mkdir -p "$PREFIX"
if [ "$(readlink -f "$SOURCE_DIR")" = "$(readlink -f "$PREFIX")" ]; then
    info "Исходники уже в $PREFIX — копирование не требуется"
else
    cp -a "$SOURCE_DIR/stacksentry" "$SOURCE_DIR/install" "$PREFIX/"
fi
# Копия установщика лежит рядом с ботом: `stacksentry-menu update` берёт её локально
# и обновляет сама себя при каждом запуске.
cp -a "$SOURCE_DIR/install.sh" "$PREFIX/install.sh" 2>/dev/null || true
cp -a "$SOURCE_DIR/requirements.txt" \
    "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$SOURCE_DIR/.env.example" \
    "$PREFIX/" 2>/dev/null || true
# Код и venv — только root: иначе скомпрометированный бот перепишет сам себя
# и переживёт рестарт. Сервисному пользователю принадлежит лишь изменяемое
# состояние: база и логи (это ровно то, что разрешает юнит).
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"
chmod 755 "$PREFIX"

_installed_version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' \
    "$PREFIX/stacksentry/__init__.py" 2>/dev/null | head -1)"
{
    printf 'VERSION=%s\n' "${_installed_version:-unknown}"
    printf 'REF=%s\n' "$REPO_REF"
    printf 'SHA=%s\n' "$INSTALLED_SHA"
    printf 'INSTALLED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'REPO=%s/%s\n' "$REPO_OWNER" "$REPO_NAME"
    printf 'ARCHIVE_SHA256=%s\n' "$SOURCE_SHA256"
} > "$PREFIX/.stacksentry-install"
chown root:root "$PREFIX/.stacksentry-install"
chmod 0644 "$PREFIX/.stacksentry-install"

# .env мастер пишет ещё до копирования — возвращаем ему владельца и права,
# которые сбросил chown -R выше (сервису нужно только чтение).
if [ -f "$ENV_FILE" ]; then
    chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
fi

# Каталог состояния создаёт и пересоздаёт systemd (StateDirectory=), но на
# этапе установки он нужен уже сейчас — самопроверка пишет в базу.
install -d -m 0750 -o "$SERVICE_USER" -g "${SERVICE_GROUP:-$SERVICE_USER}" "$STATE_DIR"
# Общая очередь запросов на обновление из Mini App: бот и webapp в одной группе.
install -d -m 0770 -o "$SERVICE_USER" -g "${SERVICE_GROUP:-$SERVICE_USER}" \
    "$STATE_DIR/update"
# setgid: файлы из webapp наследуют группу бота, а не webapp.
chmod 2770 "$STATE_DIR/update"
# Root-воркеры (apt/release) пишут только в отдельный root-owned каталог.
install -d -m 0700 -o root -g root "$STATE_DIR/root"
rm -f "$STATE_DIR"/apt-*.status "$STATE_DIR"/apt-*.packages "$STATE_DIR"/apt-*.log 2>/dev/null || true
rm -rf "$STATE_DIR/release-upgrade" 2>/dev/null || true
ok "Файлы на месте (код — root, состояние — $SERVICE_USER в $STATE_DIR)"

step "Создаём venv и ставим Python-зависимости"

if [ ! -x "$PREFIX/.venv/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$PREFIX/.venv" || fail "Не удалось создать venv"
    ok "Окружение создано"
else
    info "Окружение уже есть, обновляем пакеты"
fi
"$PREFIX/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 \
    || warn "Не удалось обновить pip, продолжаем"
info "python-telegram-bot, httpx, psutil…"
"$PREFIX/.venv/bin/pip" install -q -r "$PREFIX/requirements.txt" >/dev/null 2>&1 \
    || fail "Не удалось поставить зависимости из requirements.txt"
"$PREFIX/.venv/bin/python" -c 'import telegram, httpx, psutil, dotenv' \
    || fail "Зависимости установились, но не импортируются — проверьте venv"
# umask 077 нужен для секретов, но venv создаётся уже после него и получил бы
# права 700 (только root). Сервисный пользователь обязан уметь запускать
# интерпретатор, поэтому возвращаем чтение и исполнение для всех.
chmod -R a+rX "$PREFIX/.venv"
ok "Зависимости установлены"

# ---------------------------------------------------------------- конфиг

# ------------------------------------------------------- sudo-обёртка

step "Настраиваем привилегированные операции (sudo-обёртка)"

install -o root -g root -m 0755 "$PREFIX/install/stacksentry-ctl" "$WRAPPER_PATH" \
    || fail "Не удалось поставить $WRAPPER_PATH"
# Консольная панель для быстрых правок настроек (sudo stacksentry-menu).
install -o root -g root -m 0755 "$PREFIX/install/stacksentry-menu" "$MENU_PATH" \
    || fail "Не удалось поставить $MENU_PATH"
# Самодиагностика: sudo stacksentry-doctor
install -o root -g root -m 0755 "$PREFIX/install/stacksentry-doctor" "$DOCTOR_PATH" \
    || fail "Не удалось поставить $DOCTOR_PATH"

# visudo лежит в sbin и не всегда в PATH у не-login root-сессии.
VISUDO="$(command -v visudo 2>/dev/null || true)"
if [ -z "$VISUDO" ]; then
    for candidate_visudo in /usr/sbin/visudo /sbin/visudo /usr/local/sbin/visudo; do
        if [ -x "$candidate_visudo" ]; then
            VISUDO="$candidate_visudo"
            break
        fi
    done
fi

SUDOERS_FILE="$SUDOERS_PATH"
# Правила перечисляем ТОЧНО, без шаблонов: классический sudo допускает `*` в
# аргументах, а sudo-rs (Ubuntu 26.04+) отвечает «wildcards are not allowed in
# command arguments». Юниты берём из SERVICES, ключи команд — из
# COMMAND_ALLOWLIST (пробел → «_»), поэтому файл остаётся строгим и коротким.
SUDO_UNITS="$(env_get SERVICES | tr ',' ' ')"
# Миграция старого короткого списка на расширенный безопасный набор.
_OLD_ALLOW="journalctl --disk-usage,systemctl list-timers --all,free -h,df -h,uptime"
_NEW_ALLOW="os_logs,os_errors,kernel_logs,xui_logs,dmesg,sockets,net_summary,addresses,routes,failed_units,xui_service,uname,top_cpu,log_size,inodes,upgradable,upgrade_sim,vmstat,time_status,logged_in,boot_time,cpu_info,block_devices,journalctl --disk-usage,systemctl list-timers --all,free -h,df -h,uptime,x-ui status"
if [ "$(env_get COMMAND_ALLOWLIST)" = "$_OLD_ALLOW" ]; then
    set_env COMMAND_ALLOWLIST "$_NEW_ALLOW"
    info "Список безопасных команд расширен (старое значение заменено)."
fi

# После любых set_env возвращаем .env владельца/права: сервис читает его сам.
chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$ENV_FILE" 2>/dev/null || true
chmod 640 "$ENV_FILE" 2>/dev/null || true

SUDO_KEYS="$(env_get COMMAND_ALLOWLIST | tr ',' '\n' | sed 's/^ *//; s/ *$//' \
    | tr ' ' '_' | tr '\n' ' ')"
[ -n "$SUDO_UNITS" ] || SUDO_UNITS="$SERVICE_NAME"

sudoers_rules() {
    local rules="" unit sub key
    for unit in $SUDO_UNITS; do
        for sub in is_active restart_service journal journal_more journal_errors; do
            rules="$rules${rules:+, }$WRAPPER_PATH $sub $unit"
        done
    done
    for sub in \
        apt_update apt_upgrade apt_upgrade_phased dist_upgrade release_upgrade \
        update_bot reboot; do
        rules="$rules${rules:+, }$WRAPPER_PATH $sub"
    done
    for kind in update upgrade phased release; do
        rules="$rules${rules:+, }$WRAPPER_PATH apt_status $kind"
    done
    for key in $SUDO_KEYS; do
        rules="$rules${rules:+, }$WRAPPER_PATH run_command $key"
    done
    # Варианты логов для кнопок «Ещё 200 строк»/«Только ошибки»: read-only.
    for key in os_logs_more os_logs_errors xui_logs_more xui_logs_errors \
        kernel_logs_more kernel_logs_errors dmesg_errors; do
        rules="$rules${rules:+, }$WRAPPER_PATH run_command $key"
    done
    printf '%s' "$rules"
}
{
    printf '# Сгенерировано установщиком бота мониторинга 3X-UI.\n'
    printf '# Точные команды без шаблонов: совместимо с sudo и sudo-rs.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$SERVICE_USER" "$(sudoers_rules)"
} > "$SUDOERS_FILE"
chown root:root "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"

if [ -z "$VISUDO" ]; then
    warn "visudo не найден — синтаксис sudoers не проверен (файл собран из шаблона)"
elif _visudo_out="$("$VISUDO" -cf "$SUDOERS_FILE" 2>&1)"; then
    ok "Правила sudo валидны (проверено visudo)"
else
    # Печатаем вывод visudo как есть: обрезать его нельзя, теряется причина.
    warn "visudo ответил:"
    printf '%s\n' "$_visudo_out" | sed 's/^/        /'
    warn "версия: $("$VISUDO" -V 2>&1 | head -1)"
    info "Пробую упрощённые правила — без строки Defaults (совместимость со сборками sudo)."
    grep -vE '^(Defaults|# Defaults$)' "$SUDOERS_FILE" > "$SUDOERS_FILE.tmp" 2>/dev/null || true
    install -o root -g root -m 0440 "$SUDOERS_FILE.tmp" "$SUDOERS_FILE"
    rm -f "$SUDOERS_FILE.tmp"
    if _visudo_out2="$("$VISUDO" -cf "$SUDOERS_FILE" 2>&1)"; then
        ok "Правила sudo валидны (упрощённый вариант, без Defaults)"
    else
        # Файл в sudoers.d держать нельзя (sudo сломается), но и терять нельзя:
        # сохраняем рядом с суффиксом .invalid (sudo такие игнорирует).
        mv -f "$SUDOERS_FILE" "${SUDOERS_FILE}.invalid" 2>/dev/null || true
        warn "Правила не приняты сборкой sudo. Что прислать в issue:"
        printf '        %s\n' "visudo (raw):"
        printf '%s\n' "$_visudo_out2" | sed 's/^/          /'
        printf '        %s\n' "содержимое ${SUDOERS_FILE}.invalid:"
        sudo_cat="$(cat "${SUDOERS_FILE}.invalid" 2>/dev/null || echo '(нет файла)')"
        printf '%s\n' "$sudo_cat" | sed 's/^/          /'
        fail "Синтаксис sudoers невалиден — правила отключены, sudo не тронут."
    fi
fi
ok "Обёртка $WRAPPER_PATH установлена"

# --------------------------------------------------------------- сервис

if [ "$INIT" = systemd ]; then
    step "Готовим systemd-юнит"

    UNIT_FILE="$UNIT_PATH"
    sed -e "s#^User=.*#User=${SERVICE_USER}#" \
        -e "s#^Group=.*#Group=${SERVICE_GROUP:-$SERVICE_USER}#" \
        -e "s#/opt/stacksentry#${PREFIX}#g" \
        -e "s#/usr/local/bin/stacksentry-ctl#${WRAPPER_PATH}#g" \
        -e "s#/var/lib/stacksentry#${STATE_DIR}#g" \
        "$PREFIX/install/${SERVICE_NAME}.service" > "$UNIT_FILE"
    if [ "$STATE_DIR" != "/var/lib/stacksentry" ]; then
        # StateDirectory умеет создавать только /var/lib/<name>; для своего
        # каталога его отключаем — каталог уже создан установщиком.
        sed -i "s#^StateDirectory=.*#\# StateDirectory отключён: каталог создан установщиком#" "$UNIT_FILE"
    fi
    chmod 644 "$UNIT_FILE"
    # Отдельные постоянные юниты для apt. systemd-run в daemon-контексте на
    # новых systemd (Ubuntu 26.04) уходит в polkit и отбивается «требуется
    # интерактивная аутентификация» — transient-юнит создать нельзя. А обычный
    # юнит с User=root запускается через systemctl и работает.
    for _pair in \
        "stacksentry-apt-update:_apt_update_worker:package index update" \
        "stacksentry-apt-upgrade:_apt_worker:package upgrade" \
        "stacksentry-apt-phased:_apt_worker_phased:APT phased upgrade" \
        "stacksentry-release-upgrade:_release_upgrade_worker:do-release-upgrade" \
        "stacksentry-self-update:_self_update_worker:StackSentry self-update"; do
        _apt_unit="${_pair%%:*}"
        _rest="${_pair#*:}"
        _apt_worker="${_rest%%:*}"
        _apt_title="${_rest#*:}"
        cat > "/etc/systemd/system/${_apt_unit}.service" <<EOF
[Unit]
Description=${_apt_title} for ${SERVICE_NAME} (one-shot, requested by the bot)
After=network-online.target

[Service]
Type=oneshot
Environment=STATE_DIR=${STATE_DIR}
ExecStart=${WRAPPER_PATH} ${_apt_worker}
EOF
        chmod 644 "/etc/systemd/system/${_apt_unit}.service"
    done
    systemctl daemon-reload
    systemctl preset "$SERVICE_NAME" >/dev/null 2>&1 || true
    if systemctl enable "$SERVICE_NAME" >/dev/null 2>&1; then
        ok "Автозапуск включён: бот поднимется сам после перезагрузки"
    elif [ "$SYSTEMD_ACTIVE" -eq 1 ]; then
        fail "Не удалось включить автозапуск (systemctl enable $SERVICE_NAME)"
    else
        warn "Автозапуск не включён: хост загружен не через systemd"
    fi

    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        ok "Автозапуск: enabled"
    elif [ "$SYSTEMD_ACTIVE" -eq 0 ]; then
        warn "Автозапуск не проверить: хост загружен не через systemd"
    else
        warn "Автозапуск не подтверждён: systemctl is-enabled $SERVICE_NAME"
    fi

    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "$UNIT_FILE" >/dev/null 2>&1 \
            || warn "systemd-analyze нашёл замечания к юниту (посмотрите: systemd-analyze verify $UNIT_FILE)"
    fi
else
    step "Ставим init-скрипт ($INIT)"

    sed -e "s#@SERVICE_USER@#${SERVICE_USER}#g" \
        -e "s#@STATE_DIR@#${STATE_DIR}#g" \
        -e "s#^PREFIX=.*#PREFIX=${PREFIX}#" \
        "$PREFIX/install/${SERVICE_NAME}.init" > "$INIT_SCRIPT" \
        || fail "Не удалось записать $INIT_SCRIPT"
    chmod 0755 "$INIT_SCRIPT"

    case "$INIT" in
        openrc)
            rc-update add "$SERVICE_NAME" default >/dev/null 2>&1 \
                || warn "rc-update не сработал — добавьте автозапуск вручную"
            ;;
        sysv)
            if command -v update-rc.d >/dev/null 2>&1; then
                update-rc.d "$SERVICE_NAME" defaults >/dev/null 2>&1 \
                    || warn "update-rc.d не сработал — добавьте автозапуск вручную"
            elif command -v chkconfig >/dev/null 2>&1; then
                chkconfig --add "$SERVICE_NAME" >/dev/null 2>&1 \
                    || warn "chkconfig не сработал — добавьте автозапуск вручную"
            else
                warn "нет update-rc.d/chkconfig — автозапуск настройте вручную"
            fi
            ;;
    esac
    ok "Служба зарегистрирована: $INIT_SCRIPT"
fi


# ------------------------------------------------------------- терминал

install_terminal() {
    step "Терминал (опционально)"
    if [ "$INIT" != "systemd" ]; then
        warn "Терминал ставится только на systemd; для $INIT пропускаю"
        return 0
    fi
    TERM_USER="stacksentry-term"
    TERM_HOME="/var/lib/stacksentry-term"
    TERM_ENV_DIR="/etc/stacksentry"
    TERM_ENV="$TERM_ENV_DIR/terminal.env"
    TERM_SECRETS="$TERM_ENV_DIR/terminal-secrets.env"
    TERM_UNIT="/etc/systemd/system/stacksentry-terminal.service"
    TERM_SUDOERS="/etc/sudoers.d/stacksentry-terminal"

    if ! id "$TERM_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir "$TERM_HOME" \
            --shell /bin/bash --comment "StackSentry terminal" "$TERM_USER" \
            || { warn "Не удалось создать $TERM_USER"; return 1; }
    fi
    TERM_GROUP="$(id -gn "$TERM_USER")"
    install -d -m 0750 -o "$TERM_USER" -g "$TERM_GROUP" "$TERM_HOME"
    # Отдельный пользователь для обычного shell: у него НЕТ sudo-правила,
    # иначе из обычного терминала можно было бы сделать sudo -i.
    TERM_SHELL_USER="stacksentry-shell"
    TERM_SHELL_HOME="/var/lib/stacksentry-shell"
    if ! id "$TERM_SHELL_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir "$TERM_SHELL_HOME" \
            --shell /bin/bash --comment "StackSentry terminal shell" "$TERM_SHELL_USER" \
            || { warn "Не удалось создать $TERM_SHELL_USER"; return 1; }
    fi
    TERM_SHELL_GROUP="$(id -gn "$TERM_SHELL_USER")"
    install -d -m 0750 -o "$TERM_SHELL_USER" -g "$TERM_SHELL_GROUP" "$TERM_SHELL_HOME"
    install -d -m 0750 -o root -g root "$TERM_ENV_DIR"

    # SSH-ключ терминала: используется для локального SSH-подключения к
    # 127.0.0.1. Приватный ключ читает только сервис терминала, снаружи он не
    # выдаётся. Если sshd/ssh-keyscan недоступны, терминал остаётся на PTY.
    #
    # Ключ лежит в домашнем каталоге сервиса, а не в /etc/stacksentry:
    # /etc/stacksentry закрыт для служебного пользователя, и старый путь
    # приводил к PermissionError и 502 при старте терминала.
    OLD_TERM_SSH_DIR="$TERM_ENV_DIR/terminal-ssh"
    TERM_SSH_DIR="$TERM_HOME/terminal-ssh"
    TERM_SSH_KEY="$TERM_SSH_DIR/id_ed25519"
    TERM_SSH_KNOWN_HOSTS="$TERM_SSH_DIR/known_hosts"
    TERM_SSH_KEY_VALUE=""
    install -d -m 0700 -o "$TERM_USER" -g "$TERM_GROUP" "$TERM_SSH_DIR"
    # Обновление со старой раскладки: переносим уже работающий ключ, чтобы
    # не менять authorized_keys и не ломать SSH-вход.
    if [ "$OLD_TERM_SSH_DIR" != "$TERM_SSH_DIR" ] \
        && [ ! -f "$TERM_SSH_KEY" ] \
        && [ -f "$OLD_TERM_SSH_DIR/id_ed25519" ]; then
        cp -a "$OLD_TERM_SSH_DIR/id_ed25519" "$TERM_SSH_KEY" 2>/dev/null || true
        if [ -f "$OLD_TERM_SSH_DIR/id_ed25519.pub" ]; then
            cp -a "$OLD_TERM_SSH_DIR/id_ed25519.pub" \
                "$TERM_SSH_KEY.pub" 2>/dev/null || true
        fi
        if [ -f "$OLD_TERM_SSH_DIR/known_hosts" ]; then
            cp -a "$OLD_TERM_SSH_DIR/known_hosts" \
                "$TERM_SSH_KNOWN_HOSTS" 2>/dev/null || true
        fi
    fi
    if command -v ssh-keygen >/dev/null 2>&1; then
        if [ ! -f "$TERM_SSH_KEY" ]; then
            ssh-keygen -q -t ed25519 -N '' -C "stacksentry-terminal" \
                -f "$TERM_SSH_KEY" \
                || warn "Не удалось сгенерировать SSH-ключ терминала"
        fi
        if [ -f "$TERM_SSH_KEY.pub" ]; then
            # OpenSSH отказывается использовать ключ с групповым/чужим
            # доступом, поэтому приватный ключ принадлежит сервису и 0600.
            chown "$TERM_USER":"$TERM_GROUP" "$TERM_SSH_KEY"
            chown "$TERM_USER":"$TERM_GROUP" "$TERM_SSH_KEY.pub"
            chmod 600 "$TERM_SSH_KEY"
            chmod 644 "$TERM_SSH_KEY.pub"
            install -d -m 0700 -o "$TERM_USER" -g "$TERM_GROUP" "$TERM_HOME/.ssh"
            touch "$TERM_HOME/.ssh/authorized_keys"
            if ! grep -qxF "$(cat "$TERM_SSH_KEY.pub")" \
                "$TERM_HOME/.ssh/authorized_keys" 2>/dev/null; then
                cat "$TERM_SSH_KEY.pub" >> "$TERM_HOME/.ssh/authorized_keys"
            fi
            chown "$TERM_USER":"$TERM_GROUP" "$TERM_HOME/.ssh/authorized_keys"
            chmod 600 "$TERM_HOME/.ssh/authorized_keys"
            # Пароль не задаём: вход только по ключу. '*' не блокирует
            # key-only вход, в отличие от '!' у системных пользователей.
            usermod -p '*' "$TERM_USER" >/dev/null 2>&1 || true
            if command -v ssh-keyscan >/dev/null 2>&1 \
                && ssh-keyscan -T 3 -t ed25519 -p 22 127.0.0.1 \
                    > "$TERM_SSH_KNOWN_HOSTS.tmp" 2>/dev/null \
                && [ -s "$TERM_SSH_KNOWN_HOSTS.tmp" ]; then
                mv "$TERM_SSH_KNOWN_HOSTS.tmp" "$TERM_SSH_KNOWN_HOSTS"
                chown "$TERM_USER":"$TERM_GROUP" "$TERM_SSH_KNOWN_HOSTS"
                chmod 600 "$TERM_SSH_KNOWN_HOSTS"
                # Проверяем именно вход по ключу, а не только доступность порта.
                if command -v ssh >/dev/null 2>&1 && timeout 5 ssh \
                    -i "$TERM_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
                    -o StrictHostKeyChecking=yes \
                    -o UserKnownHostsFile="$TERM_SSH_KNOWN_HOSTS" \
                    -p 22 "$TERM_USER@127.0.0.1" true >/dev/null 2>&1; then
                    TERM_SSH_KEY_VALUE="$TERM_SSH_KEY"
                else
                    warn "sshd не принял ключ — терминал будет работать через PTY"
                fi
            else
                rm -f "$TERM_SSH_KNOWN_HOSTS.tmp"
                warn "SSH на 127.0.0.1 недоступен — терминал будет работать через PTY"
            fi
        fi
    else
        warn "ssh-keygen не найден — терминал будет работать через PTY"
    fi

    TERM_BOT_TOKEN="$(env_get TELEGRAM_BOT_TOKEN)"
    TERM_ADMIN_IDS="$(env_get TELEGRAM_ADMIN_IDS)"
    if [ -z "$TERM_BOT_TOKEN" ] || [ -z "$TERM_ADMIN_IDS" ]; then
        warn "Нет TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_IDS — терминал пропущен"
        return 1
    fi
    TERM_ACCESS_USER="${TERM_ADMIN_IDS%%,*}"
    _old_secret="$(sed -n 's/^TERMINAL_SECRET=//p' "$TERM_SECRETS" 2>/dev/null | head -1)"
    TERM_TICKET_SECRET="${_old_secret:-$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))')}"
    _old_access="$(sed -n 's/^TERMINAL_ACCESS_TOKEN=//p' "$TERM_SECRETS" 2>/dev/null | head -1)"
    TERM_ACCESS_TOKEN="${_old_access:-$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')}"
    _old_totp="$(sed -n 's/^TERMINAL_TOTP_SECRET=//p' "$TERM_SECRETS" 2>/dev/null | head -1)"
    TERM_TOTP="${_old_totp:-$("$PYTHON_BIN" -c 'import base64,secrets; print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="))')}"
    # Новый секрет — старую отметку «QR уже показан» сбрасываем.
    [ -n "$_old_totp" ] || rm -f "$TERM_HOME/totp-enrolled"
    if [ "$TERMINAL_ORIGIN_EXPLICIT" -eq 0 ]; then
        TERMINAL_ORIGIN="$(sed -n 's/^TERMINAL_PUBLIC_ORIGIN=//p' "$TERM_ENV" 2>/dev/null | head -1)"
        case "$TERMINAL_ORIGIN" in
            https://term.*.sslip.io|https://term.*.sslip.io:*)
                TERMINAL_PUBLIC=1
                ;;
        esac
    fi
    if [ "$ROOT_TERMINAL_EXPLICIT" -eq 0 ]; then
        _old_root="$(sed -n 's/^TERMINAL_ALLOW_ROOT=//p' "$TERM_ENV" 2>/dev/null | head -1)"
        [ -n "$_old_root" ] && TERMINAL_ALLOW_ROOT="$_old_root"
    fi

    if [ -z "$TERMINAL_ORIGIN" ] && [ "$TERMINAL_ORIGIN_EXPLICIT" -eq 0 ] \
        && [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        if [ "$TERMINAL_PUBLIC" -eq 1 ]; then
            info "Терминал будет опубликован вместе с Mini App."
        elif [ "$WEBAPP" -eq 1 ] && [ "$WEBAPP_AUTO_HTTPS" != "0" ] \
            && [ "$WEBAPP_ORIGIN_EXPLICIT" -eq 0 ]; then
            ask "Открыть терминал внутри Mini App? [Y/n] "
            read -r _terminal_public <&$READ_FD || _terminal_public=""
            case "$_terminal_public" in
                ""|y|Y|yes|да|д) TERMINAL_PUBLIC=1 ;;
                *) TERMINAL_PUBLIC=0 ;;
            esac
            if [ "$TERMINAL_PUBLIC" -eq 1 ]; then
                info "Вход в терминал: Telegram + TOTP."
            fi
        else
            question TERMINAL_ORIGIN \
                "Публичный HTTPS-адрес терминала (Enter — только localhost/SSH-туннель)" ""
            [ -n "$TERMINAL_ORIGIN" ] && TERMINAL_ORIGIN_EXPLICIT=1
        fi
    fi
    case "$TERMINAL_ORIGIN" in
        "") ;;
        https://*) TERMINAL_ORIGIN="${TERMINAL_ORIGIN%/}" ;;
        *) warn "TERMINAL_ORIGIN должен начинаться с https:// — игнорирую"; TERMINAL_ORIGIN="" ;;
    esac

    cat > "$TERM_SECRETS" <<EOF
TELEGRAM_BOT_TOKEN=$TERM_BOT_TOKEN
TELEGRAM_ADMIN_IDS=$TERM_ADMIN_IDS
TERMINAL_SECRET=$TERM_TICKET_SECRET
TERMINAL_TOTP_SECRET=$TERM_TOTP
TERMINAL_ACCESS_TOKEN=$TERM_ACCESS_TOKEN
TERMINAL_ACCESS_USER=$TERM_ACCESS_USER
EOF
    chown root:root "$TERM_SECRETS"
    chmod 600 "$TERM_SECRETS"

    TERM_WEBAPP_ORIGIN="$(env_get WEBAPP_PUBLIC_ORIGIN)"
    TERM_FRAME_ANCESTORS="'self' https://web.telegram.org https://*.telegram.org"
    [ -n "$TERM_WEBAPP_ORIGIN" ] \
        && TERM_FRAME_ANCESTORS="$TERM_FRAME_ANCESTORS $TERM_WEBAPP_ORIGIN"

    cat > "$TERM_ENV" <<EOF
TERMINAL_ALLOW_ROOT=$TERMINAL_ALLOW_ROOT
TERMINAL_SHELL_USER=$TERM_SHELL_USER
TERMINAL_SHELL_HOME=$TERM_SHELL_HOME
TERMINAL_HOST=127.0.0.1
TERMINAL_PORT=8788
TERMINAL_PUBLIC_ORIGIN=$TERMINAL_ORIGIN
TERMINAL_FRAME_ANCESTORS="$TERM_FRAME_ANCESTORS"
TERMINAL_SESSION_TTL=30
TERMINAL_IDLE_TIMEOUT=900
TERMINAL_MAX_SESSIONS=1
TERMINAL_SSH_KEY=$TERM_SSH_KEY_VALUE
TERMINAL_SSH_USER=$TERM_USER
TERMINAL_SSH_HOST=127.0.0.1
TERMINAL_SSH_PORT=22
TERMINAL_SSH_KNOWN_HOSTS=$TERM_SSH_KNOWN_HOSTS
TERMINAL_ENV=$TERM_ENV
TERMINAL_REPLAY_DB=$TERM_HOME/replay.db
TERMINAL_TOTP_ENROLLED=$TERM_HOME/totp-enrolled
EOF
    chown root:root "$TERM_ENV"
    chmod 600 "$TERM_ENV"

    cat > "$TERM_UNIT" <<EOF
[Unit]
Description=StackSentry — web terminal (TOTP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TERM_USER
Group=$TERM_GROUP
WorkingDirectory=$PREFIX
EnvironmentFile=$TERM_ENV
EnvironmentFile=$TERM_SECRETS
Environment=HOME=$TERM_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$PREFIX/.venv/bin/uvicorn --factory stacksentry.terminal.server:create_app_from_env --host 127.0.0.1 --port 8788 --no-access-log
Restart=on-failure
RestartSec=5
# Это настоящий shell: ProtectSystem/ProtectHome сломали бы запись и /root,
# поэтому изоляция ограничена отдельным пользователем и сетевым периметром.
PrivateTmp=true
NoNewPrivileges=false
RestrictSUIDSGID=false
UMask=0077
MemoryMax=256M
TasksMax=64

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$TERM_UNIT"
    systemctl daemon-reload

    printf '%s ALL=(%s) NOPASSWD: /bin/bash -l\n' \
        "$TERM_USER" "$TERM_SHELL_USER" > "$TERM_SUDOERS"
    if [ "$TERMINAL_ALLOW_ROOT" -eq 1 ]; then
        printf '%s ALL=(root) NOPASSWD: /bin/bash -l\n' "$TERM_USER" >> "$TERM_SUDOERS"
    fi
    chown root:root "$TERM_SUDOERS"
    chmod 440 "$TERM_SUDOERS"
    if command -v visudo >/dev/null 2>&1 \
        && ! visudo -cf "$TERM_SUDOERS" >/dev/null 2>&1; then
        # Пробуем без root-правила: shell-переключение важнее.
        printf '%s ALL=(%s) NOPASSWD: /bin/bash -l\n' \
            "$TERM_USER" "$TERM_SHELL_USER" > "$TERM_SUDOERS"
        chmod 440 "$TERM_SUDOERS"
        if ! visudo -cf "$TERM_SUDOERS" >/dev/null 2>&1; then
            rm -f "$TERM_SUDOERS"
            warn "visudo не принял правила терминала — терминал не установлен"
            return 1
        fi
        TERMINAL_ALLOW_ROOT=0
        sed -i 's/^TERMINAL_ALLOW_ROOT=.*/TERMINAL_ALLOW_ROOT=0/' "$TERM_ENV"
        warn "root-правило не принято — root-терминал выключен"
    fi

    systemctl enable stacksentry-terminal >/dev/null 2>&1 || true
    if [ "$START_SERVICE" -eq 1 ]; then
        systemctl restart stacksentry-terminal || warn "Не удалось запустить stacksentry-terminal"
        sleep 2
        if systemctl is-active --quiet stacksentry-terminal; then
            ok "Терминал запущен на 127.0.0.1:8788"
        else
            warn "Терминал не активен: journalctl -u stacksentry-terminal -n 30"
        fi
    else
        info "Терминал установлен, запуск пропущен (--no-start)"
    fi

    info "Вход: Telegram + код TOTP из приложения-аутентификатора."
    if [ -t 1 ]; then
        printf '        TOTP secret: %s\n' "$TERM_TOTP"
        printf '        otpauth: otpauth://totp/3X-UI%%20Sentry:root?secret=%s&issuer=3X-UI%%20Sentry\n' "$TERM_TOTP"
        printf '        access token (вне Telegram): %s\n' "$TERM_ACCESS_TOKEN"
    else
        info "Секреты: sudo cat $TERM_SECRETS (только root)"
    fi
    if [ -n "$TERMINAL_ORIGIN" ]; then
        info "Публичный адрес: $TERMINAL_ORIGIN"
    else
        info "Доступ только локально. С ноутбука:"
        info "  ssh -L 8788:127.0.0.1:8788 root@<сервер>"
        info "  затем http://127.0.0.1:8788"
    fi
    if [ "$TERMINAL_ALLOW_ROOT" -eq 1 ]; then
        warn "ROOT-терминал включён (sudo -i). Держите его за VPN/SSH-туннелем."
    else
        info "Root выключен. Включить: --root-terminal."
    fi
    return 0
}


# ------------------------------------------------------- автоматический HTTPS
# Если домена нет, Mini App всё равно можно поднять «из коробки»: берём
# публичный IPv4, строим бесплатный sslip.io-хост и просим Caddy выпустить
# сертификат Let's Encrypt. Пользователю остаётся только /setdomain.

detect_public_ipv4() {
    WEBAPP_AUTO_IP=""
    local url ip
    for url in \
        https://api.ipify.org \
        https://ifconfig.me/ip \
        https://icanhazip.com
    do
        if command -v curl >/dev/null 2>&1; then
            ip="$(curl -4fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
        elif command -v wget >/dev/null 2>&1; then
            ip="$(wget -qO- --timeout=5 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
        else
            return 1
        fi
        [ -n "$ip" ] || continue
        if "$PYTHON_BIN" - "$ip" <<'PY' >/dev/null 2>&1
import ipaddress
import sys

try:
    addr = ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if addr.is_global else 1)
PY
        then
            WEBAPP_AUTO_IP="$ip"
            return 0
        fi
    done
    return 1
}

webapp_port_is_free() {
    "$PYTHON_BIN" - "$1" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

webapp_open_firewall() {
    local port
    for port in 80 "$1"; do
        if command -v ufw >/dev/null 2>&1 \
            && ufw status 2>/dev/null | grep -qi '^Status: active'; then
            ufw allow "${port}/tcp" >/dev/null 2>&1 || true
        fi
        if command -v firewall-cmd >/dev/null 2>&1 \
            && firewall-cmd --state >/dev/null 2>&1; then
            firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null 2>&1 || true
            firewall-cmd --reload >/dev/null 2>&1 || true
        fi
    done
}

webapp_https_preflight() {
    WEBAPP_PREFLIGHT_OK=0
    WEBAPP_PREFLIGHT_IP=""
    WEBAPP_PREFLIGHT_HOST=""
    WEBAPP_PREFLIGHT_PORT=""
    WEBAPP_PREFLIGHT_ORIGIN=""
    WEBAPP_PREFLIGHT_TERMINAL_ORIGIN=""
    WEBAPP_PREFLIGHT_REASON=""
    WEBAPP_PREFLIGHT_CADDY_INSTALLED=0

    local caddyfile
    if [ "${WEBAPP_AUTO_HTTPS:-1}" = "0" ]; then
        WEBAPP_PREFLIGHT_REASON="авто-HTTPS отключён (--no-webapp-auto)"
        return 1
    fi
    if ! command -v curl >/dev/null 2>&1; then
        WEBAPP_PREFLIGHT_REASON="нужен curl"
        return 1
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        WEBAPP_PREFLIGHT_REASON="нужен systemd"
        return 1
    fi

    caddyfile="/etc/caddy/Caddyfile"
    if command -v caddy >/dev/null 2>&1; then
        WEBAPP_PREFLIGHT_CADDY_INSTALLED=1
        if [ -f "$caddyfile" ] \
            && ! grep -q 'StackSentry Mini App' "$caddyfile" 2>/dev/null \
            && [ ! -f /etc/caddy/.stacksentry-managed ]; then
            if [ "$WEBAPP_CADDY_ADOPT" -eq 1 ]; then
                :
            elif [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
                warn "Caddy уже установлен, но конфиг не от StackSentry."
                ask "Заменить конфиг Caddy для StackSentry? Старый сохранится. [y/N] "
                read -r _caddy_adopt <&$READ_FD || _caddy_adopt=""
                case "$_caddy_adopt" in
                    y|Y|yes|да|д) WEBAPP_CADDY_ADOPT=1 ;;
                    *)
                        WEBAPP_PREFLIGHT_REASON="Caddy уже установлен со своей конфигурацией"
                        return 1
                        ;;
                esac
            else
                WEBAPP_PREFLIGHT_REASON="Caddy уже установлен со своей конфигурацией"
                return 1
            fi
        fi
    fi

    if ! detect_public_ipv4; then
        WEBAPP_PREFLIGHT_REASON="не удалось определить публичный IPv4"
        return 1
    fi
    WEBAPP_PREFLIGHT_IP="$WEBAPP_AUTO_IP"
    WEBAPP_PREFLIGHT_HOST="${WEBAPP_AUTO_IP//./-}.sslip.io"

    if [ "$WEBAPP_PREFLIGHT_CADDY_INSTALLED" -eq 1 ]; then
        if grep -q 'https://[^ ]*:8443' "$caddyfile" 2>/dev/null; then
            WEBAPP_PREFLIGHT_PORT=8443
            WEBAPP_PREFLIGHT_REASON="443 занят — использую 8443"
        else
            WEBAPP_PREFLIGHT_PORT=443
        fi
    else
        if ! webapp_port_is_free 80; then
            WEBAPP_PREFLIGHT_REASON="порт 80 занят"
            return 1
        fi
        if webapp_port_is_free 443; then
            WEBAPP_PREFLIGHT_PORT=443
        elif webapp_port_is_free 8443; then
            WEBAPP_PREFLIGHT_PORT=8443
            WEBAPP_PREFLIGHT_REASON="443 занят — использую 8443"
        else
            WEBAPP_PREFLIGHT_REASON="порты 443 и 8443 заняты"
            return 1
        fi
    fi

    if [ "$WEBAPP_PREFLIGHT_PORT" = "443" ]; then
        WEBAPP_PREFLIGHT_ORIGIN="https://${WEBAPP_PREFLIGHT_HOST}"
    else
        WEBAPP_PREFLIGHT_ORIGIN="https://${WEBAPP_PREFLIGHT_HOST}:${WEBAPP_PREFLIGHT_PORT}"
    fi
    if [ "$TERMINAL" -eq 1 ] && [ "$TERMINAL_PUBLIC" -eq 1 ] \
        && [ "$TERMINAL_ORIGIN_EXPLICIT" -eq 0 ] \
        && [ -f /etc/systemd/system/stacksentry-terminal.service ]; then
        if [ "$WEBAPP_PREFLIGHT_PORT" = "443" ]; then
            WEBAPP_PREFLIGHT_TERMINAL_ORIGIN="https://term.${WEBAPP_PREFLIGHT_HOST}"
        else
            WEBAPP_PREFLIGHT_TERMINAL_ORIGIN="https://term.${WEBAPP_PREFLIGHT_HOST}:${WEBAPP_PREFLIGHT_PORT}"
        fi
    fi
    WEBAPP_PREFLIGHT_OK=1
    return 0
}

webapp_ask_manual_origin() {
    local url
    while :; do
        ask "  Публичный HTTPS-адрес Mini App: "
        read -r url <&$READ_FD || url=""
        url="${url%/}"
        case "$url" in
            "")
                warn "Адрес не может быть пустым. Пример: https://sentry.example.com"
                ;;
            https://?*)
                if printf '%s' "$url" | grep -q '[[:space:]]'; then
                    warn "В адресе не должно быть пробелов."
                else
                    WEBAPP_ORIGIN="$url"
                    WEBAPP_ORIGIN_EXPLICIT=1
                    WEBAPP_AUTO_HTTPS=0
                    info "Использую $url"
                    return 0
                fi
                ;;
            *)
                warn "Нужен адрес, начинающийся с https:// (например, https://sentry.example.com)"
                ;;
        esac
    done
}

webapp_wizard() {
    local choice default
    printf '\n%s%s%s\n' "$BOLD$BLUE" "Mini App в Telegram" "$RESET"
    info "Mini App — это страница StackSentry, которую Telegram открывает по HTTPS."

    if webapp_https_preflight; then
        info "Публичный IP: ${WEBAPP_PREFLIGHT_IP}"
        [ -n "$WEBAPP_PREFLIGHT_REASON" ] \
            && info "$WEBAPP_PREFLIGHT_REASON"
        info "Автоматический адрес: ${WEBAPP_PREFLIGHT_ORIGIN}"
        [ -n "$WEBAPP_PREFLIGHT_TERMINAL_ORIGIN" ] \
            && info "Терминал: ${WEBAPP_PREFLIGHT_TERMINAL_ORIGIN}"
        default=1
    else
        [ -n "$WEBAPP_PREFLIGHT_IP" ] \
            && info "Публичный IP: ${WEBAPP_PREFLIGHT_IP}"
        warn "Автоматический HTTPS недоступен: ${WEBAPP_PREFLIGHT_REASON}"
        default=3
    fi

    printf '\n  Как открывать Mini App?\n'
    if [ "$WEBAPP_PREFLIGHT_OK" -eq 1 ]; then
        printf '    1) Автоматически — %s  (рекомендуется)\n' \
            "$WEBAPP_PREFLIGHT_ORIGIN"
    else
        printf '    1) Автоматически — недоступно\n'
    fi
    printf '    2) Свой домен и готовый HTTPS\n'
    printf '    3) Только локально / SSH-туннель\n'

    while :; do
        ask "  Выбор [$default]: "
        read -r choice <&$READ_FD || choice=""
        choice="${choice:-$default}"
        case "$choice" in
            1)
                if [ "$WEBAPP_PREFLIGHT_OK" -eq 1 ]; then
                    WEBAPP_ORIGIN=""
                    WEBAPP_ORIGIN_EXPLICIT=0
                    WEBAPP_AUTO_HTTPS=1
                    info "HTTPS настрою автоматически после установки."
                    return 0
                fi
                warn "Автоматический вариант недоступен: ${WEBAPP_PREFLIGHT_REASON}"
                ;;
            2) webapp_ask_manual_origin; return 0 ;;
            3)
                WEBAPP_ORIGIN=""
                WEBAPP_ORIGIN_EXPLICIT=1
                WEBAPP_AUTO_HTTPS=0
                info "Mini App будет доступен только локально, через SSH-туннель."
                return 0
                ;;
            *) warn "Введите 1, 2 или 3." ;;
        esac
    done
}

webapp_fallback() {
    local choice
    [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ] || return 0

    warn "Автоматический HTTPS не настроен."
    printf '\n  Что делать?\n'
    printf '    1) Повторить\n'
    printf '    2) Указать свой домен\n'
    printf '    3) Оставить только локально\n'

    while :; do
        ask "  Выбор [3]: "
        read -r choice <&$READ_FD || choice=""
        choice="${choice:-3}"
        case "$choice" in
            1)
                WEBAPP_AUTO_HTTPS=1
                WEBAPP_ORIGIN=""
                WEBAPP_ORIGIN_EXPLICIT=0
                WEBAPP_PREFLIGHT_OK=0
                webapp_auto_https && return 0
                ;;
            2) webapp_ask_manual_origin; return 0 ;;
            3)
                WEBAPP_ORIGIN=""
                WEBAPP_ORIGIN_EXPLICIT=1
                WEBAPP_AUTO_HTTPS=0
                info "Mini App будет доступен только локально, через SSH-туннель."
                return 0
                ;;
            *) warn "Введите 1, 2 или 3." ;;
        esac
    done
}

webapp_set_terminal_origin() {
    local origin="$1"
    local env_file="/etc/stacksentry/terminal.env"

    [ -n "$origin" ] || return 0
    [ -f "$env_file" ] || return 0
    if grep -q '^TERMINAL_PUBLIC_ORIGIN=' "$env_file"; then
        sed -i "s|^TERMINAL_PUBLIC_ORIGIN=.*|TERMINAL_PUBLIC_ORIGIN=$origin|" \
            "$env_file"
    else
        printf 'TERMINAL_PUBLIC_ORIGIN=%s\n' "$origin" >> "$env_file"
    fi
    chown root:root "$env_file" 2>/dev/null || true
    chmod 600 "$env_file" 2>/dev/null || true
    TERMINAL_ORIGIN="$origin"
    systemctl try-restart stacksentry-terminal >/dev/null 2>&1 || true
}

webapp_auto_https() {
    WEBAPP_AUTO_ORIGIN=""
    WEBAPP_AUTO_TERMINAL_ORIGIN=""
    local host port origin site term_host term_site term_origin
    local backup code i install_ok=0
    local caddyfile="/etc/caddy/Caddyfile"

    if [ "$WEBAPP_PREFLIGHT_OK" -ne 1 ]; then
        if ! webapp_https_preflight; then
            warn "Авто-HTTPS: ${WEBAPP_PREFLIGHT_REASON:-недоступен}"
            return 1
        fi
    fi

    host="$WEBAPP_PREFLIGHT_HOST"
    port="$WEBAPP_PREFLIGHT_PORT"

    if command -v caddy >/dev/null 2>&1; then
        WEBAPP_PREFLIGHT_CADDY_INSTALLED=1
    fi

    if [ "$WEBAPP_PREFLIGHT_CADDY_INSTALLED" -eq 0 ]; then
        info "Ставлю Caddy для автоматического HTTPS..."
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive $PKG_INSTALL caddy >/dev/null 2>&1 \
            && install_ok=1
        if [ "$install_ok" -eq 0 ] && [ -n "$PKG_UPDATE" ]; then
            # Пакет мог отсутствовать в устаревших списках — обновляем и пробуем ещё раз.
            # shellcheck disable=SC2086
            $PKG_UPDATE >/dev/null 2>&1 || true
            # shellcheck disable=SC2086
            DEBIAN_FRONTEND=noninteractive $PKG_INSTALL caddy >/dev/null 2>&1 \
                && install_ok=1
        fi
        if [ "$install_ok" -eq 0 ] || ! command -v caddy >/dev/null 2>&1; then
            warn "Пакет caddy недоступен — установите его вручную или задайте --webapp-origin"
            return 1
        fi
        WEBAPP_PREFLIGHT_CADDY_INSTALLED=1
    fi

    if [ "$port" = "443" ]; then
        site="$host"
    else
        site="$host:$port"
    fi
    origin="https://$site"

    term_site=""
    term_origin=""
    if [ -n "$WEBAPP_PREFLIGHT_TERMINAL_ORIGIN" ]; then
        term_host="term.$host"
        term_origin="$WEBAPP_PREFLIGHT_TERMINAL_ORIGIN"
        if [ "$port" = "443" ]; then
            term_site="$term_host"
        else
            term_site="$term_host:$port"
        fi
    fi

    if [ "$WEBAPP_PREFLIGHT_CADDY_INSTALLED" -eq 1 ] \
        && [ -f "$caddyfile" ] \
        && ! grep -q 'StackSentry Mini App' "$caddyfile" 2>/dev/null \
        && [ ! -f /etc/caddy/.stacksentry-managed ] \
        && [ "$WEBAPP_CADDY_ADOPT" -ne 1 ]; then
        warn "Caddy уже установлен со своей конфигурацией — авто-HTTPS пропущен"
        return 1
    fi

    mkdir -p /etc/caddy
    backup=""
    if [ -f "$caddyfile" ]; then
        backup="${caddyfile}.stacksentry.bak"
        cp -a "$caddyfile" "$backup" 2>/dev/null || backup=""
    fi
    cat > "$caddyfile" <<EOF
# StackSentry Mini App — generated automatically. Do not edit.
{
    auto_https disable_redirects
}

https://$site {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8787
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
    }
}
EOF
    if [ -n "$term_site" ]; then
        cat >> "$caddyfile" <<EOF

https://$term_site {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8788
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
    }
}
EOF
    fi
    chown root:caddy "$caddyfile" 2>/dev/null || chown root:root "$caddyfile"
    chmod 644 "$caddyfile"
    touch /etc/caddy/.stacksentry-managed
    webapp_open_firewall "$port"

    systemctl enable caddy >/dev/null 2>&1 || true
    systemctl restart caddy >/dev/null 2>&1 || true
    sleep 2
    if ! systemctl is-active --quiet caddy; then
        warn "Caddy не запустился: journalctl -u caddy -n 50"
        if [ -n "$backup" ]; then
            cp -a "$backup" "$caddyfile" 2>/dev/null || true
            systemctl restart caddy >/dev/null 2>&1 || true
        fi
        return 1
    fi

    info "Жду сертификат Let's Encrypt для $host (до 60 секунд)..."
    i=0
    while [ "$i" -lt 30 ]; do
        i=$((i + 1))
        code="$(curl -4sS --max-time 5 --resolve "$host:$port:127.0.0.1" \
            -o /dev/null -w '%{http_code}' "$origin/" 2>/dev/null || true)"
        if [ -n "$code" ] && [ "$code" != "000" ]; then
            ok "Автоматический HTTPS готов: $origin"
            if [ -n "$term_site" ]; then
                info "Жду сертификат для $term_host (до 60 секунд)..."
                i=0
                while [ "$i" -lt 30 ]; do
                    i=$((i + 1))
                    code="$(curl -4sS --max-time 5 \
                        --resolve "$term_host:$port:127.0.0.1" \
                        -o /dev/null -w '%{http_code}' "$term_origin/" \
                        2>/dev/null || true)"
                    if [ -n "$code" ] && [ "$code" != "000" ]; then
                        ok "Терминал в Mini App готов: $term_origin"
                        webapp_set_terminal_origin "$term_origin"
                        WEBAPP_AUTO_TERMINAL_ORIGIN="$term_origin"
                        break
                    fi
                    sleep 2
                done
                if [ -z "$WEBAPP_AUTO_TERMINAL_ORIGIN" ]; then
                    warn "Сертификат для терминала не получен: journalctl -u caddy -n 50"
                fi
            fi
            WEBAPP_AUTO_ORIGIN="$origin"
            return 0
        fi
        sleep 2
    done

    warn "Сертификат не получен. Проверьте: journalctl -u caddy -n 50"
    return 1
}

# ------------------------------------------------------------- Mini App

install_webapp() {
    step "Mini App (read-only)"
    if [ "$INIT" != "systemd" ]; then
        warn "Mini App ставится только на systemd; для $INIT пропускаю"
        return 0
    fi
    if [ -z "$WEBAPP_ORIGIN" ] && [ "$WEBAPP_ORIGIN_EXPLICIT" -eq 0 ]; then
        WEBAPP_ORIGIN="$(env_get WEBAPP_PUBLIC_ORIGIN)"
    fi
    if [ -z "$WEBAPP_ORIGIN" ] && [ "$WEBAPP_ORIGIN_EXPLICIT" -eq 0 ] \
        && [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        webapp_wizard
    fi
    if [ -z "$WEBAPP_ORIGIN" ] && [ "$WEBAPP_ORIGIN_EXPLICIT" -eq 0 ] \
        && [ "$WEBAPP_AUTO_HTTPS" != "0" ]; then
        if ! webapp_auto_https; then
            webapp_fallback
        fi
        [ -n "$WEBAPP_AUTO_ORIGIN" ] && WEBAPP_ORIGIN="$WEBAPP_AUTO_ORIGIN"
    fi
    if [ -z "$WEBAPP_ORIGIN" ] && [ "$WEBAPP_ORIGIN_EXPLICIT" -eq 0 ] \
        && [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        webapp_ask_manual_origin
    fi
    case "$WEBAPP_ORIGIN" in
        "") ;;
        https://*) WEBAPP_ORIGIN="${WEBAPP_ORIGIN%/}" ;;
        *) warn "WEBAPP_ORIGIN должен начинаться с https:// — игнорирую"; WEBAPP_ORIGIN="" ;;
    esac
    if [ "$TERMINAL_PUBLIC" -eq 1 ] && [ -z "$WEBAPP_AUTO_TERMINAL_ORIGIN" ] \
        && [ -z "$TERMINAL_ORIGIN" ] && [ -n "$WEBAPP_ORIGIN" ] \
        && [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        ask "Публичный HTTPS-адрес терминала (Enter — без вкладки): "
        read -r _terminal_manual <&$READ_FD || _terminal_manual=""
        case "$_terminal_manual" in
            "") ;;
            https://*) webapp_set_terminal_origin "${_terminal_manual%/}" ;;
            *) warn "Нужен адрес с https:// — вкладка терминала не добавлена." ;;
        esac
    fi
    WEBAPP_SECRET_VALUE="$(env_get WEBAPP_SECRET)"
    if [ -z "$WEBAPP_SECRET_VALUE" ]; then
        WEBAPP_SECRET_VALUE="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))')"
    fi
    set_env WEBAPP_ENABLED 1
    set_env WEBAPP_SECRET "$WEBAPP_SECRET_VALUE"
    set_env WEBAPP_HOST 127.0.0.1
    set_env WEBAPP_PORT 8787
    set_env WEBAPP_PUBLIC_ORIGIN "$WEBAPP_ORIGIN"
    set_env WEBAPP_SESSION_TTL 3600

    # Вкладка терминала внутри Mini App: нужен публичный HTTPS-адрес
    # терминала и разрешение ему быть в iframe на origin Mini App.
    TERM_ENV_FILE="/etc/stacksentry/terminal.env"
    TERM_SERVICE_FILE="/etc/systemd/system/stacksentry-terminal.service"
    TERM_PUBLIC_ORIGIN=""
    TERM_INSTALLED=0
    if [ -f "$TERM_ENV_FILE" ] && [ -f "$TERM_SERVICE_FILE" ]; then
        TERM_INSTALLED=1
        TERM_PUBLIC_ORIGIN="$(sed -n 's/^TERMINAL_PUBLIC_ORIGIN=//p' \
            "$TERM_ENV_FILE" 2>/dev/null | head -1)"
        if [ -n "$WEBAPP_ORIGIN" ]; then
            TERM_FRAME_ANCESTORS="'self' https://web.telegram.org https://*.telegram.org $WEBAPP_ORIGIN"
            if grep -q '^TERMINAL_FRAME_ANCESTORS=' "$TERM_ENV_FILE"; then
                sed -i "s|^TERMINAL_FRAME_ANCESTORS=.*|TERMINAL_FRAME_ANCESTORS=\"$TERM_FRAME_ANCESTORS\"|" \
                    "$TERM_ENV_FILE"
            else
                printf 'TERMINAL_FRAME_ANCESTORS="%s"\n' \
                    "$TERM_FRAME_ANCESTORS" >> "$TERM_ENV_FILE"
            fi
        fi
    fi
    set_env WEBAPP_TERMINAL_ORIGIN "$TERM_PUBLIC_ORIGIN"
    if [ "$TERM_INSTALLED" -eq 1 ]; then
        systemctl try-restart stacksentry-terminal >/dev/null 2>&1 || true
    fi

    if id "$WEBAPP_USER" >/dev/null 2>&1; then
        ok "Пользователь Mini App $WEBAPP_USER уже есть"
    elif [ "$WEBAPP_USER" = "root" ]; then
        fail "WEBAPP_USER=root — Mini App получил бы полные права"
    else
        useradd --system --no-create-home --home-dir "$WEBAPP_HOME" \
            --shell "$NOLOGIN_SHELL" --comment "StackSentry Mini App" \
            "$WEBAPP_USER" || fail "Не удалось создать пользователя $WEBAPP_USER"
        ok "Создан отдельный пользователь Mini App: $WEBAPP_USER"
    fi
    WEBAPP_GROUP="$(id -gn "$WEBAPP_USER" 2>/dev/null || printf '%s' "$WEBAPP_USER")"
    # .env принадлежит root:SERVICE_GROUP (0640). Webapp читает его только как
    # член этой группы, но не получает прав на sudo-обёртку или ключи терминала.
    usermod -aG "${SERVICE_GROUP:-$SERVICE_USER}" "$WEBAPP_USER" 2>/dev/null || true
    # Только чтение журналов служб для drill-down в Mini App.
    if getent group systemd-journal >/dev/null 2>&1; then
        usermod -aG systemd-journal "$WEBAPP_USER" 2>/dev/null || true
    fi
    install -d -m 0750 -o "$WEBAPP_USER" -g "$WEBAPP_GROUP" "$WEBAPP_HOME"
    # Каталог мог остаться от прерванного запуска/старой раскладки.
    chown -R "$WEBAPP_USER":"$WEBAPP_GROUP" "$WEBAPP_HOME" 2>/dev/null || true
    chmod 0750 "$WEBAPP_HOME" 2>/dev/null || true

    cat > /etc/systemd/system/stacksentry-webapp.service <<EOF
[Unit]
Description=StackSentry — read-only Mini App
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$WEBAPP_USER
Group=$WEBAPP_GROUP
SupplementaryGroups=${SERVICE_GROUP:-$SERVICE_USER}
WorkingDirectory=$PREFIX
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
# Своя база: веб-слой не пишет в аудит/состояние бота. Не STATE_PATH:
# он уже занят в .env бота, а EnvironmentFile имеет приоритет над Environment.
Environment=WEBAPP_STATE_PATH=$WEBAPP_HOME/state.db
ExecStart=$PREFIX/.venv/bin/uvicorn --factory stacksentry.webapp:create_app_from_env --host 127.0.0.1 --port 8787 --no-access-log
Restart=on-failure
RestartSec=5
ProtectSystem=strict
ReadWritePaths=$WEBAPP_HOME $STATE_DIR/update
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
UMask=0077
MemoryMax=256M
TasksMax=64

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 /etc/systemd/system/stacksentry-webapp.service
    systemctl daemon-reload
    systemctl enable stacksentry-webapp >/dev/null 2>&1 || true
    if [ "$START_SERVICE" -eq 1 ]; then
        systemctl restart stacksentry-webapp || warn "Не удалось запустить stacksentry-webapp"
        sleep 2
        if systemctl is-active --quiet stacksentry-webapp; then
            ok "Mini App запущен на 127.0.0.1:8787"
        else
            warn "Mini App не активен: journalctl -u stacksentry-webapp -n 30"
        fi
    else
        info "Mini App установлен, запуск пропущен (--no-start)"
    fi
    if [ -n "$WEBAPP_ORIGIN" ]; then
        _webapp_domain="${WEBAPP_ORIGIN#https://}"
        _webapp_domain="${_webapp_domain%%:*}"
        info "Публичный адрес Mini App: $WEBAPP_ORIGIN"
        if [ -n "$WEBAPP_AUTO_ORIGIN" ]; then
            info "HTTPS настроен автоматически: sslip.io + Caddy + Let's Encrypt."
        fi
        if [ -n "$WEBAPP_AUTO_TERMINAL_ORIGIN" ]; then
            info "Терминал внутри Mini App: $WEBAPP_AUTO_TERMINAL_ORIGIN (Telegram + TOTP)"
        elif [ -n "$TERM_PUBLIC_ORIGIN" ]; then
            info "Терминал внутри Mini App: $TERM_PUBLIC_ORIGIN (Telegram + TOTP)"
        elif [ -f /etc/systemd/system/stacksentry-terminal.service ]; then
            info "Терминал: только локально, через SSH-туннель."
        fi
        info "Адрес Mini App: $WEBAPP_ORIGIN"
        info "Инструкция для @BotFather будет показана в самом конце установки."
    else
        info "Доступ только локально. С ноутбука:"
        info "  ssh -L 8787:127.0.0.1:8787 root@<сервер>"
        info "  затем http://127.0.0.1:8787"
    fi
    info "Read-only: просмотр состояния, служб, панели, трафика, подписок и истории."
    return 0
}

if [ "$TERMINAL" -eq 1 ]; then
    install_terminal || warn "Терминал не установлен"
fi

if [ "$WEBAPP" -eq 1 ]; then
    install_webapp || warn "Mini App не установлен"
fi

if [ "$START_SERVICE" -eq 1 ]; then
    step "Запускаем бота"

    if [ "$INIT" = systemd ]; then
        if systemctl restart "$SERVICE_NAME"; then
            sleep 4
            systemctl is-active --quiet "$SERVICE_NAME" \
                || {
                    warn "Сервис не активен, последние строки журнала:"
                    journalctl -u "$SERVICE_NAME" -n 15 --no-pager | sed 's/^/        /'
                    fail "Бот не стартовал. Подробности: journalctl -u $SERVICE_NAME -f"
                }
            ok "Сервис запущен"
        else
            fail "systemctl restart не отработал: journalctl -u $SERVICE_NAME -n 30 --no-pager"
        fi
    elif "$INIT_SCRIPT" start; then
        ok "Служба запущена"
    else
        fail "Не удалось запустить: $INIT_SCRIPT start (лог: $STATE_DIR/bot.log)"
    fi
else
    step "Запуск пропущен"
    if [ "$INIT" = systemd ]; then
        info "Запустить позже: systemctl enable --now $SERVICE_NAME"
    else
        info "Запустить позже: $INIT_SCRIPT start"
    fi
fi

# ------------------------------------------------------------ самопроверка

step "Проверяем, что всё работает"

PY="$PREFIX/.venv/bin/python"
CFG_OK=0
# .env НЕ подключаем через source: значения вроде COMMAND_ALLOWLIST содержат
# пробелы, и оболочка приняла бы хвост значения за команду. Бот читает файл сам.
if (cd "$PREFIX" && "$PY" -c 'from stacksentry.settings import load_settings; s = load_settings(); print(s.api_base, s.telegram_token[:6])' >/dev/null 2>&1); then
    check_ok "Конфигурация читается ботом без ошибок"
    CFG_OK=1
else
    check_fail "Конфигурация не проходит проверку — откройте $ENV_FILE редактором (содержимое содержит токены: не пересылайте его третьим лицам)"
fi

# 2FA обязательна, если от неё не отказались явно через --no-2fa. Так
# пропущенный или прерванный мастер не остаётся незамеченным.
_2fa_required_check="$(env_get TOTP_REQUIRED)"
_2fa_secret_check="$(env_get TOTP_SECRET)"
if [ "$_2fa_required_check" = "1" ] && [ -n "$_2fa_secret_check" ]; then
    check_ok "2FA включена: Mini App и опасные действия требуют TOTP"
elif [ "$_2fa_required_check" = "1" ]; then
    check_fail "2FA включена, но TOTP_SECRET пуст — выполните: sudo bash ${PREFIX}/install.sh --2fa"
elif [ "$_2fa_required_check" = "0" ]; then
    check_ok "2FA отключена — TOTP не требуется"
else
    check_fail "2FA не настроена — выберите: sudo bash ${PREFIX}/install.sh --2fa (или --no-2fa)"
fi

TG_TOKEN_VALUE="$(env_get TELEGRAM_BOT_TOKEN)"
TG_API_URL_VALUE="$(env_get TELEGRAM_API_URL)"
[ -n "$TG_API_URL_VALUE" ] || TG_API_URL_VALUE="https://api.telegram.org"
# Без -f: на HTTP 401 тело ответа («Unauthorized») как раз нужно для диагноза.
TG_RESULT="$(curl_secure "" "${TG_API_URL_VALUE%/}/bot${TG_TOKEN_VALUE}/getMe" \
    -sS --max-time 15 2>/dev/null || true)"
if printf '%s' "$TG_RESULT" | grep -q '"ok":true'; then
    TG_NAME="$(printf '%s' "$TG_RESULT" | sed -E 's/.*"username":"([^"]+)".*/\1/')"
    check_ok "Telegram отвечает, бот @$TG_NAME"
else
    case "$TG_RESULT" in
        *401*|*"Unauthorized"*)
            check_fail "Токен @BotFather отклонён Telegram — впишите правильный TELEGRAM_BOT_TOKEN" ;;
        *)
            check_fail "Не удалось проверить токен Telegram (нет сети или он неверный)" ;;
    esac
fi

if [ "$CFG_OK" -eq 1 ]; then
    PANEL_API="$(
        cd "$PREFIX" || exit 1
        "$PY" -c 'from stacksentry.settings import load_settings; print(load_settings().api_base)' 2>/dev/null || true
    )"
    PANEL_TOKEN_VALUE="$(env_get PANEL_API_TOKEN)"
    if [ -z "$(env_get PANEL_URL)" ] && [ -z "$(env_get PANEL_API_TOKEN)" ]; then
        check_ok "API панели не настроен — monitor-only (службы наблюдаются)"
    elif [ -n "$PANEL_API" ] && [ -n "$PANEL_TOKEN_VALUE" ]; then
        # Без -f: HTTP-код нужен, чтобы отличить «токен не принят» (401/403)
        # от «путь не тот» (404) и «панель недоступна» (000/сеть).
        PANEL_HTTP_DIR="$(mktemp -d)"
        panel_http() {
            curl_secure "Authorization: Bearer ${PANEL_TOKEN_VALUE}" \
                "${PANEL_API}/$1" \
                -sS --max-time 15 -o "$PANEL_HTTP_DIR/body" -w '%{http_code}' \
                2>/dev/null || echo 000
        }
        STATUS_CODE="$(panel_http server/status)"
        PANEL_VER="$(sed -n 's/.*"panelVersion":"\([^"]*\)".*/\1/p' "$PANEL_HTTP_DIR/body" | head -1)"
        if [ "$STATUS_CODE" = "200" ] && grep -q '"success":true' "$PANEL_HTTP_DIR/body"; then
            if [ -n "$PANEL_VER" ]; then
                check_ok "API панели принимает токен (3X-UI v$PANEL_VER, $PANEL_API/server/status)"
            else
                check_ok "API панели принимает токен ($PANEL_API/server/status)"
            fi
        else
            case "$STATUS_CODE" in
                401|403)
                    check_fail "Панель отклонила токен (HTTP $STATUS_CODE) — значение неверное или у токена нет нужного scope. Создайте токен со scope node-sync (или admin): 3X-UI v3 — «Настройки панели → Учётная запись → API-токен» (значение видно один раз, копируйте сразу); 3X-UI 2.x — «Настройки → API Tokens»." ;;
                404)
                    check_fail "Панель ответила 404 на $PANEL_API/server/status — проверьте PANEL_URL и PANEL_BASE_PATH. Помните: PANEL_BASE_PATH — это webBasePath панели, он входит в адрес как префикс перед /panel/api." ;;
                000)
                    check_fail "Панель недоступна по $PANEL_API — проверьте PANEL_URL, порт и что служба панели запущена" ;;
                *)
                    check_fail "Панель вернула HTTP $STATUS_CODE на $PANEL_API/server/status: $(head -c 160 "$PANEL_HTTP_DIR/body")" ;;
            esac
        fi

        if [ "$STATUS_CODE" = "200" ]; then
            INBOUND_CODE="$(panel_http inbounds/list)"
            if [ "$INBOUND_CODE" = "200" ] && grep -q '"success":true' "$PANEL_HTTP_DIR/body"; then
                check_ok "Список inbound'ов и подписок доступен"
            else
                check_fail "inbounds/list недоступен (HTTP $INBOUND_CODE) — токену нужен scope node-sync или admin"
            fi
        fi
        rm -rf "$PANEL_HTTP_DIR"
    else
        check_fail "Не удалось вычислить адрес API панели"
    fi

    WRITE_OUT="$(
        cd "$PREFIX" || exit 1
        sudo -u "$SERVICE_USER" "$PY" -c '
from stacksentry.settings import load_settings
from stacksentry.storage import Storage
s = load_settings()
db = Storage(s.state_path)
db.audit("installer_selfcheck", telegram_id=None, ok=True, result="ok")
rows = db.recent_audit(1)
db.close()
print(rows[0]["result"] if rows else "empty")
' 2>&1 || true
    )"
    WRITE_RESULT="$(printf '%s' "$WRITE_OUT" | head -1)"
    if [ "$WRITE_RESULT" = "ok" ]; then
        check_ok "Бот может писать состояние в $(env_get STATE_PATH)"
    else
        check_fail "Нет записи в $(env_get STATE_PATH): $(printf '%s' "$WRITE_OUT" | tail -1 | head -c 200)"
    fi
fi

SUDO_RESULT="$(sudo -u "$SERVICE_USER" "$WRAPPER_PATH" is_active x-ui 2>&1 || true)"
case "$SUDO_RESULT" in
    active)   check_ok "sudo-обёртка работает (юнит x-ui: active)" ;;
    inactive) check_ok "sudo-обёртка работает (юнит x-ui: inactive — запустите панель)" ;;
    *)        check_fail "sudo-обёртка не отвечает: $SUDO_RESULT" ;;
esac

if [ "$INIT" = systemd ]; then
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        check_ok "Автозапуск: systemctl is-enabled $SERVICE_NAME = enabled"
    elif [ "$SYSTEMD_ACTIVE" -eq 0 ]; then
        check_note "Автозапуск не проверен: хост загружен не через systemd"
    else
        check_fail "Автозапуск выключен: systemctl enable $SERVICE_NAME"
    fi
elif ls /etc/runlevels/*/"$SERVICE_NAME" /etc/rc*.d/*"$SERVICE_NAME" >/dev/null 2>&1; then
    check_ok "Автозапуск: init-скрипт зарегистрирован ($INIT)"
else
    check_note "Автозапуск не подтверждён: добавьте $SERVICE_NAME в ($INIT) вручную"
fi

if [ "$INIT" = systemd ]; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        check_ok "Сервис активен"
    elif [ "$START_SERVICE" -eq 0 ]; then
        check_ok "Сервис не запущен — так и просили (--no-start), автозапуск включён"
        info "Запустить вручную: systemctl start $SERVICE_NAME"
    elif [ "$SYSTEMD_ACTIVE" -eq 0 ]; then
        check_note "Состояние сервиса не проверено: хост загружен не через systemd"
    else
        check_fail "Сервис не активен: systemctl status $SERVICE_NAME"
    fi
elif "$INIT_SCRIPT" status >/dev/null 2>&1; then
    check_ok "Сервис активен"
elif [ "$START_SERVICE" -eq 0 ]; then
    check_ok "Сервис не запущен — так и просили (--no-start), автозапуск включён"
    info "Запустить вручную: $INIT_SCRIPT start"
else
    check_fail "Сервис не активен: $INIT_SCRIPT status"
fi

LOG_VIEW="journalctl -u ${SERVICE_NAME} -n 50"

step "Итог самопроверки"

if [ "$FAILURES" -eq 0 ]; then
    printf '\n%s%sВсе %d проверок пройдены. Бот работает.%s\n' \
        "$BOLD" "$GREEN" "$CHECKS" "$RESET"
else
    printf '\n%s%sПройдено %d из %d проверок.%s\n' "$BOLD" "$YELLOW" \
        "$((CHECKS - FAILURES))" "$CHECKS" "$RESET"
    info "Бот установлен и запущен. Что чинить — в списке выше и в $LOG_VIEW"
fi

# ---------------------------------------------------------------- итог

printf '\n%s%sГотово%s\n' "$BOLD" "$GREEN" "$RESET"
cat <<EOF

  Бот:        systemctl status ${SERVICE_NAME}
  Журнал:     journalctl -u ${SERVICE_NAME} -f
  Конфиг:     ${ENV_FILE}
  Состояние:  $(env_get STATE_PATH)
  Логи:       $(env_get LOG_PATH)

  Что дальше:
    • откройте бота в Telegram и нажмите /start
    • /status — сводка, /subs — подписки, /maint — обслуживание
    • токены лежат в ${ENV_FILE}: TELEGRAM_BOT_TOKEN (от @BotFather) и PANEL_API_TOKEN (панель)
    • после правки конфига: systemctl restart ${SERVICE_NAME}

  Обновление бота:
    sudo bash ${PREFIX}/install.sh --prefix ${PREFIX}
  Меню настроек:
    sudo stacksentry-menu            # правки настроек, рестарт, журнал, обновление

  Удаление бота:
    sudo bash ${PREFIX}/install.sh --uninstall

EOF
# ------------------------------------------------ инструкция для @BotFather

if [ "$WEBAPP" -eq 1 ] && [ -n "${WEBAPP_ORIGIN:-}" ]; then
    BOTFATHER_DOMAIN="${WEBAPP_ORIGIN#https://}"
    BOTFATHER_DOMAIN="${BOTFATHER_DOMAIN%%/*}"
    BOTFATHER_DOMAIN="${BOTFATHER_DOMAIN%%:*}"
    BOTFATHER_USER="${TG_NAME:-${TG_USERNAME:-}}"
    BOTFATHER_FILE="/etc/stacksentry/botfather.txt"

    if [ -n "$BOTFATHER_USER" ]; then
        BOTFATHER_BOT="@$BOTFATHER_USER"
    else
        BOTFATHER_BOT="своего бота в списке"
    fi

    cat > "$BOTFATHER_FILE" <<EOF
Один ручной шаг в Telegram
==========================

1. Откройте Telegram.
2. Найдите @BotFather.
3. Отправьте команду: /setdomain
4. Выберите бота: $BOTFATHER_BOT
5. Отправьте ровно эту строку:

$BOTFATHER_DOMAIN

Без https://, без /, без порта.
6. Если BotFather попросит подтверждение — подтвердите.
7. Вернитесь в терминал. Кнопка Mini App появится после перезапуска бота.

Адрес Mini App: $WEBAPP_ORIGIN
EOF
    chmod 600 "$BOTFATHER_FILE" 2>/dev/null || true

    "$PY" - "$BOTFATHER_BOT" "$BOTFATHER_DOMAIN" "$WEBAPP_ORIGIN" <<'PY'
import sys

bot, domain, origin = sys.argv[1:4]
lines = [
    "ВАЖНО: ОДИН РУЧНОЙ ШАГ В TELEGRAM",
    "",
    "Без этого шага кнопка Mini App не появится.",
    "",
    "1. Откройте Telegram.",
    "2. Найдите @BotFather.",
    "3. Отправьте команду: /setdomain",
    f"4. Выберите бота: {bot}",
    "5. Отправьте ровно эту строку:",
    "",
    f"      {domain}",
    "",
    "   Без https://, без /, без порта.",
    "6. Если BotFather попросит подтверждение — подтвердите.",
    "7. Вернитесь в терминал.",
]
width = max(len(line) for line in lines) + 4
border = "+" + "-" * (width - 2) + "+"
print()
print(border)
for index, line in enumerate(lines):
    if index == 0:
        print("| " + line.center(width - 4) + " |")
    else:
        print("| " + line.ljust(width - 4) + " |")
print(border)
print(f"\nАдрес Mini App: {origin}")
PY
    info "Эта же инструкция сохранена: $BOTFATHER_FILE"

    if [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        printf '\nКогда выполните шаг, нажмите Enter, чтобы завершить установку... '
        read -r _botfather_ack <&$READ_FD || true
    fi
fi
