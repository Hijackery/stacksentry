# История изменений / Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
GitHub Releases и теги не публикуются: актуальная версия в `main`. Версия
хранится в `stacksentry/__init__.py`. Обновление запускается кнопкой «Проверить обновления»
в боте, `sudo stacksentry-menu update` (пункт 24) или повторный запуск
установщика; подробности в [docs/RELEASING.md](docs/RELEASING.md).

## [1.0.0] (2026-10-21)

Версия 1.0.0 (без отдельного GitHub Release). Включает всё перечисленное ниже в
разделе `Unreleased`: мониторинг сервера/служб/VPN, восемь API-панелей,
адаптивный установщик и меню, кроссплатформенные обновления пакетов, 2FA, аудит,
read-only Mini App и web/root-терминал. Добавлены публичная документация,
проверка и установка обновлений из интерфейса, CodeQL/Dependabot и Makefile.

## [Unreleased]

Пока нет изменений после 1.1.1.

## [1.1.1] (2026-10-29)

- Исправлено обновление из бота и `stacksentry-menu update`: постоянный
  `stacksentry-self-update.service`, обновление больше не берёт старый код из
  `$PREFIX` и не подменяет только commit/version в метаданных.
- Mini App: отдельная вкладка «Обновления», бейдж, сброс кэша статики.
- Бот редактирует существующее сообщение вместо отправки нового.

## [1.1.0] (2026-10-29)

### Обновления, алерты и наблюдаемость

- Проверка обновлений StackSentry перенесена в «Помощь», добавлена в Mini App и
  сопровождается баннером и фоновым уведомлением админов.
- Алерты проходят через SQLite-outbox с повторами и дедупликацией; переход в
  аварию и восстановление подтверждаются несколькими замерами, чтобы отсечь
  одиночные всплески.
- Маршрутизация critical/warning по разным чатам и темам, тихие часы и ночной
  дайджест.
- Новые проверки: inode, failed systemd units, TLS-сертификат панели, свежесть
  бэкапа, HTTP/DNS, SMART (опционально) и прогноз заполнения диска.
- История 24 часа/7/30 дней с p95 и пиками; поиск по журналам в боте.
- Mini App: аккордеон Помощи, сохранение вкладки, JSON-логи, `/healthz`,
  Prometheus `/metrics` и команда `stacksentry-doctor`.
- 2FA: неверный код удаляется из чата, ошибка обновляет тот же экран без
  второго окна; Mini App очищает поле и показывает то же предупреждение.
- Версия 1.1.0 и проверка синхронизации версии с README/CHANGELOG.
- Mini App: drill-down служб с журналом, графики метрик, детали проверок и
  подписок, единый поиск, закладки и настройка дашборда.
- Обновление из Mini App через подтверждение в боте, этапы, прогресс-бар и
  восстановление статуса после перезапуска. Отдельная вкладка «Обновления»,
  бейдж о новой версии и сброс кэша статических файлов.

### Ребрендинг

- Продукт называется **StackSentry** (панели + VPN + службы + сервер). Технические
  идентификаторы `stacksentry`, `stacksentry-bot`, `stacksentry-menu`, `stacksentry-ctl`
  сохранены для совместимости с уже установленными ботами; полный технический
  rename: отдельным обновлением с миграцией.

### Переименование

- Проект теперь **StackSentry**: репозиторий `Hijackery/stacksentry`,
  Python-пакет `stacksentry`, служба `stacksentry-bot`, пользователь `stacksentry`,
  команды `stacksentry-menu` и `stacksentry-ctl`, состояние в `/var/lib/stacksentry`.
  Установщик автоматически переносит `.env` и базу из старой установки
  `xui-monitor-bot`, удаляет старые юниты/sudoers/обёртки и пользователя.
  Старые ссылки GitHub перенаправляются, но установщик и README уже используют
  новый адрес.

### Документация

- README (ru/en) переписан по логической цепочке: зачем → как выглядит →
  совместимость → быстрый старт → подключение панели → устройство → команды →
  `stacksentry-menu` → настройки → безопасность → обновление/удаление → диагностика →
  разработка. Добавлены содержание, таблицы совместимости и симптомов, примеры
  экранов и точные команды проверки API/пути.
- CI теперь проверяет синтаксис `stacksentry-menu` и init-скрипта, `shellcheck` для меню
  и `visudo -cf` для sudoers, сгенерированного из реальных имён юнитов и ключей.

### Возможности

- API-адаптеры панелей: 3X-UI (как раньше), Marzban (OAuth2 логин/пароль,
  пользователи/трафик, рестарт xray), Marzneshin (`/api/admins/token`,
  постраничные пользователи), PasarGuard (`/api/admin/token`, пользователи,
  рестарт core), Remnawave (Bearer API token, пользователи/трафик), S-UI
  (`Token`-заголовок, клиенты/трафик, рестарт sing-box), Hiddify
  (`Hiddify-API-Key`, пользователи/трафик) и Outline (секретный
  `SB_API_PREFIX`, access keys + `/metrics/transfer`). Переключение через
  `PANEL_TYPE`/`stacksentry-menu`; у S-UI/Hiddify/Outline путь панели входит
  в `PANEL_URL`.

- Адаптация под сервер: установщик определяет ОС, пакетный менеджер, службы и
  тип панели (3X-UI/Marzban/Remnawave/S-UI/Hiddify) и предлагает только
  подходящие поля: токен либо логин/пароль, с проверкой API. В меню добавлен
  пункт «Что найдено на сервере», тип панели и доступы Marzban; рестарт ядра
  выбирает нужный endpoint. Главное меню бота скрывает «Трафик»/«Подписки» без
  API, кнопка рестарта ядра появляется только для поддерживаемых панелей, а
  обслуживание показывает команды выбранного пакетного менеджера.

- Обновления системы без привязки к APT: `stacksentry-ctl` определяет
  `apt/dnf/yum/zypper/pacman/apk/xbps/emerge/eopkg/pkg` и выполняет
  обновление индексов и пакетов его штатными командами. Phased- и
  релиз-обновления остаются только для APT и скрываются на других системах.

- Расширенный реестр стека: S-UI, Marzban, Marzneshin, PasarGuard, Hiddify,
  Remnawave, Outline; Xray, V2Ray, sing-box, Hysteria, TUIC, Trojan, NaiveProxy,
  Mieru, ShadowTLS, Brook, mtg, Shadowsocks, Squid, Dante, 3proxy, GOST;
  WireGuard, AmneziaWG, OpenVPN, IPsec, SoftEther, Tailscale, ZeroTier, NetBird,
  Headscale, Nebula. Детект по юнитам, путям и процессам; пакеты apk/xbps/
  emerge/eopkg/pkg.

- Read-only Mini App: дашборд в Telegram (обзор, службы, панель, трафик,
  подписки, история), вход по initData, cookie HttpOnly, CSP, сервис
  `stacksentry-webapp` на 127.0.0.1:8787, кнопка «Панель» в Telegram. Без действий
  и секретов в браузере.

- Monitor-only: без API 3X-UI бот наблюдает службы, VPN-стек и железо;
  `PANEL_URL`/`PANEL_API_TOKEN` можно оставить пустыми (оба сразу), установщик
  сам определяет 3X-UI и спрашивает, настраивать ли API. `--no-panel`/`--panel`.

- 2FA (TOTP) для опасных действий бота: step-up означает просмотр без кода, рестарты и
  apt update после кода с разблокировкой на 15 минут, обновления ОС и
  перезагрузка только со свежим кодом. Код одноразовый, 3 попытки, блокировка
  на 5 минут, аудит; сообщение с кодом удаляется. Установщик спрашивает и
  печатает otpauth; `sudo stacksentry-menu totp` показывает секрет.

- Обновления в боте разделены и отчитываются в чат: `apt update`, `full-upgrade`,
  `phased`, проверка релиза (`do-release-upgrade --check`) и запуск
  `do-release-upgrade`: отдельные пункты. Бот сам следит за systemd-юнитом и
  присылает код возврата и хвост журнала; состояние задачи переживает рестарт
  бота (SQLite).
- Команду сервера можно прислать в любом разделе (или `/run <команда>`): бот
  проверяет её по белому списку, просит подтверждение и возвращает вывод.
- Новый экран `/traffic` (раньше была только кнопка).
- Меню больше не копятся: при переходе предыдущий экран заменяется (кроме
  «Подробнее»/«Свернуть», которые разворачивают тот же экран).
- `/help`: полноценная инструкция, разделы, обновления, команды, метрики,
  безопасность, диагностика и настройки.
- `stacksentry-menu apt-logs [N]`: журнал фоновых обновлений; README описывает каждый
  пункт меню: что вписать и пример значения.
- Обновление через `stacksentry-menu` переустанавливает код, обёртку, меню, юниты и
  sudoers, поэтому новые фичи применяются и при будущих обновлениях.

- Telegram-бот мониторинга: система, systemd-службы, панель 3X-UI, xray,
  inbound'ы, подписки и трафик; алерты с гистерезисом, кулдауном и мьютом
  «знаю, до утра» (хранится в SQLite).
- Безопасные действия по подтверждению: рестарт сервиса и xray, `apt`
  full/phased в transient-юните, проверка релиз-обновления, перезагрузка
  сервера, команда из allowlist.
- Инструкция внутри бота: `/help` и кнопка «❓ Помощь» с расшифровкой метрик.
- Консольная панель `stacksentry-menu`: состояние, показ и правка настроек (безопасный
  белый список ключей), рестарт, журнал, обновление, удаление.
- Меню `stacksentry-menu` оформлено рамкой (как в панели 3X-UI): пункты сгруппированы по
  разделам, сверху видно состояние службы, ширина рамки подстраивается под текст,
  неверный номер даёт подсказку с диапазоном. Добавлены подменю порогов, правка
  служб и уровня логирования и перезапуск xray в панели через API
  (`stacksentry-menu restart-xray`).
- Установка одной командой с GitHub: мастер спрашивает токены четвёртым шагом
  (до установки зависимостей), проверяет токен бота и доступность панели,
  умеет выпустить токен панели автоматически; самопроверка из 8 проверок.
- Поддержка systemd, OpenRC (Alpine, Gentoo) и SysVinit: установщик сам
  определяет менеджер служб (`--init` для явного выбора), для не-systemd систем
  ставится init-скрипт и используется файл лога вместо `journalctl`.
- Логи чтения: журнал сервиса с фильтром «🔎 Только ошибки» и добором строк,
  `/audit` с фильтрами «отказы / действия».

### Возможности

- Терминал (опционально): отдельный сервис `stacksentry-terminal`, пользователь
  `stacksentry-term`, вход по Telegram initData + TOTP, одноразовые тикеты,
  access token для доступа вне Telegram, опциональный root через точное
  sudoers-правило `/bin/bash -l`. По умолчанию слушает 127.0.0.1.
- Инвентарь панелей/VPN: 3X-UI, S-UI, Marzban, Hiddify, Xray, sing-box,
  WireGuard, AmneziaWG, OpenVPN, Shadowsocks, Tailscale; установщик сам
  подставляет SERVICES, экран `/stack` показывает найденное и наблюдаемое.
- Установщик: apk (Alpine), xbps (Void), pkg (FreeBSD, best-effort).

### Безопасность

- Аудит терминала: секреты больше не наследуются PTY-shell; обычный shell работает под
  `stacksentry-shell` без sudo, root через отдельное правило; TOTP-код нормализуется
  для replay-защиты; исправлены утечка fd, гонка карты сессий и битые cookie/
  тикеты с не-ASCII.
- Установщик: `--2fa`/`--no-2fa`/`--no-panel` работают и на готовом `.env`;
  monitor-only считается полным конфигом; повторный `--terminal`/`--webapp`
  сохраняет секреты/origin/root; `GH_TOKEN` уходит только на github.com.
- Mini App: `SameSite=None+Secure` для Telegram и `Lax` для localhost, без
  бесконечного retry; monitor-only отчёт о перезагрузке больше не врёт.

### Исправлено

- У логов из `/run` (`os_logs`, `xui_logs`, `kernel_logs`, `dmesg`) появились
  кнопки «📜 Ещё 200 строк» и «🔎 Только ошибки», как у журналов служб. Варианты
  логов разрешены, только если базовый лог есть в `COMMAND_ALLOWLIST`.
- Список безопасных команд в боте (`/run` без аргументов и кнопка «Выполнить
  команду») теперь сгруппирован и снабжён описаниями, а не только алиасами.
- После миграции `COMMAND_ALLOWLIST` файл `.env` снова получает права
  `640 root:<группа сервиса>`: `set_env` под umask 077 оставлял `root:root 600`,
  и бот падал на старте с `PermissionError: .env`. Группа сервиса теперь
  определяется через `id -gn`, а не предполагается равной имени пользователя.
- Трафик: экран показывает итоги по inbound'ам и топ клиентов по расходу; при
  пустом ответе объясняет, что проверить (scope, webBasePath, токен).
- Одно действие даёт одно сообщение: «в очереди» → «запущено» → отчёт (раньше
  копились «Запущено в фоне» и «… запущено»). Заголовки приведены в порядок.
- Убрана дублирующая кнопка проверки релиза: `do-release-upgrade` сам проверяет
  версию; «No new release found» (код 1) считается успехом, а не ошибкой.
- Алерты не объявляют «восстановлено», если сборщик не опрошен (`unknown`
  вместо `ok`); убраны дубли «Системные метрики недоступны».
- Подписки: один клиент в нескольких inbound'ах больше не дублируется, трафик
  и лимит агрегируются.
- Панель: `api_authorized` сбрасывается между опросами; `xray` строкой/словарём
  больше не роняет проверку.
- Система: сбой `DISK_PATH` или сети не обнуляет остальные метрики; `is_active`
  работает на OpenRC/SysV.
- Меню: «Обновить/В меню» не оставляют экран без клавиатуры при throttle;
  длинные HTML-сообщения режутся с закрытием/повторным открытием тегов.
- Кулдаун считается по цели (x-ui и fail2ban не блокируют друг друга), TTL и
  кулдаун на `monotonic`; несовпадение действия не сжигает подтверждение;
  имя юнита не может начинаться с `-`; `summary(0)` не возвращает весь вывод;
  неудачный рестарт xray тоже учитывается кулдауном.
- Безопасные команды расширены и документированы: логи ОС/ядра/x-ui, сеть,
  службы, обновления, железо: только чтение, без shell.

### Исправлено (старое)

- Удаление бота: отчёт теперь короткий, одна строка «Бот удалён.» вместо
  списка того, что и где стёрто (`--keep-state` по-прежнему предупреждает об
  оставшихся базе и пользователе). `stacksentry-menu` после удаления завершается, а не
  открывает меню заново, возвращаться уже некуда.
- Раздел «Обслуживание» больше не «зависает»: при таймауте команды в
  `run_command` переменные вывода не были инициализированы и задача падала с
  `UnboundLocalError` без отчёта. Теперь вывод заводится заранее, а при
  таймауте гасится вся группа процессов, внук (`do-release-upgrade` → `apt`)
  не держит пайпы и не переживает прерывание.
- Любая ошибка действия (не только `ActionError`) теперь возвращается в чат и
  пишется в аудит, а не оставляет сообщение «Выполняю…» навсегда.
- Проверка релиз-обновления: `do-release-upgrade` писал в read-only
  `PREFIX/.cache` (ProtectSystem=strict) и падал с `mkdir() failed`. Ему выдают
  записываемый `HOME`/`XDG_CACHE_HOME` в каталоге состояния, а таймаут проверки
  ограничен 120 с вместо `COMMAND_TIMEOUT`.
- Обновление системы: `systemd-run` в daemon-контексте новых systemd
  (Ubuntu 26.04) отбивается polkit («требуется интерактивная аутентификация»),
  и кнопка молча не работала. Установщик ставит постоянные юниты
  `stacksentry-apt-upgrade.service` и `stacksentry-apt-phased.service`
  (`User=root`), обёртка запускает их через `systemctl`; при их отсутствии
  transient-юнит, затем фоновый запуск.
- `webBasePath`: путь `/panel` из адресной строки не является webBasePath (в 3X-UI
  интерфейс всегда на `.../panel`, а «URL панели» задаётся отдельно, обычно `/`).
  Установщик теперь проверяет путь до запроса токена (правильный отвечает 401,
  неверный 404) и молча исправляет лишний сегмент `.../panel/panel/api`.
- При неверном токене панель отдаёт пустой 401, поэтому установщик и
  самопроверка писали «панель не ответила». Теперь различаются 401/403 (токен и
  его scope), 404 (адрес API) и недоступность, а в самопроверке виден HTTP-код.
- Юнит больше не использует `ProcSubset=pid`: он оставлял в `/proc` только
  процессы и прятал `/proc/stat`, `/proc/meminfo`, `/proc/net`, из-за этого
  psutil не мог собрать метрики и бот писал «Системные метрики недоступны».
- Проблемы железа и проблемы подписок разделены: раньше предупреждения о метриках
  попадали в находку «Подписки: нужна проверка», а одинаковые строки дублировались.

- Установка на чистой машине: каталог `/opt/stacksentry` создаётся до записи
  `.env` (иначе установщик падал на шаге 4).
- Недозаполненный `.env` после прерванного запуска больше не требует правки
  руками: мастер дозаполняет недостающие значения.
- Адрес панели: лишние пробелы и слеши убираются (`https://host:5580/`), путь из
  адреса переносится в `webBasePath`, двойной слеш невозможен ни в проверке, ни
  в адресе API.
- `asyncio.TimeoutError` на Python 3.10: таймауты команд работали только на
  3.11+; теперь ловятся оба имени, процесс действительно прерывается.
- Процессы не остаются сиротами при отмене задачи или выключении бота;
  `RetryAfter` (flood control) не теряет сообщение; кулдаун проверяется под
  блокировкой, подтверждение не расходуется чужим нажатием.
- Разметка: `render_overview` не падает на NULL-метриках, длинные сообщения
  режутся по строкам, HTML-теги не рвутся.
- Совместимость с `sudo-rs` (Ubuntu 26.04+, `visudo-rs`): правила теперь
  генерируются точным списком команд, без шаблонов в аргументах, без алиасов и
  без `Defaults`, которые sudo-rs не принимает. `journal`/`journal_errors`/
  `journal_more`: отдельные подкоманды, `run_command` получает ключ команды.
- Установщик больше не скрывает причину отказа `visudo`: строка ошибки попадает в
  вывод, а если сборка sudo не принимает расширенные `Defaults`, ставится
  упрощённый вариант правил: бот устанавливается в любом случае.

### Безопасность

- Установщик проверяет `--prefix` и `STATE_DIR`: `/`, `/etc`, `/usr`, `/var` и
  подобные пути отклоняются (обход: `ALLOW_UNSAFE_PREFIX=1`). Раньше
  `--prefix /etc` мог сделать `chmod -R` и `rm -rf` по системному каталогу.
- При заданном `--sha256` git-fallback отключён: непроверенный код не ставится.
- init-скрипт больше не выполняет `.env` как shell-скрипт; `reboot` работает
  через OpenRC/SysV; `STATE_DIR` подставляется в юнит.
- Токен панели при проверке пишется во временный `mktemp`-файл, а не в
  предсказуемый `/tmp/.xui-mon-bot-tok`.

- Секреты не попадают в логи, Telegram ID маскируются, `stacksentry-menu show/check` не
  печатают значения токенов.
- `PANEL_CA_BUNDLE` для пиннинга сертификата панели; установщик умеет сверять
  архив по `--sha256`; клиент панели не следует редиректам.
- Команды и кнопки принимаются только в личном чате администратора; отказы
  пишутся в журнал; обёртка `stacksentry-ctl` работает только по узкому sudoers-списку.
- `run_command` запускает фиксированные argv без `bash -c`; аргументы подкоманд
  валидируются (`safe_unit`, `safe_int`, запрет лишних аргументов).

---

## English

### Rebrand

- The product is now **StackSentry** (panels + VPN + services + server). The
  technical `stacksentry`, `stacksentry-bot`, `stacksentry-menu`, `stacksentry-ctl`
  identifiers are kept for compatibility with existing installs; a full technical
  rename will be a separate update with migration.

### Rename

- The project is now **StackSentry**: repository `Hijackery/stacksentry`,
  Python package `stacksentry`, service `stacksentry-bot`, user `stacksentry`, commands
  `stacksentry-menu` and `stacksentry-ctl`, state in `/var/lib/stacksentry`. The installer
  migrates `.env` and the database from the old `xui-monitor-bot` installation
  and removes the old units/sudoers/wrappers and user. Old GitHub links redirect,
  but the installer and README already use the new address.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). GitHub
Releases and tags are not published; `main` is the current version. Update via
the in-bot check/update buttons, `sudo stacksentry-menu` (item 24), or by
re-running the installer (see [docs/RELEASING.md](docs/RELEASING.md)).

### Added

- Panel API adapters: 3X-UI (unchanged), Marzban (OAuth2 username/password,
  users/traffic, xray restart) and Remnawave (Bearer API token, users/traffic).
  Switch via `PANEL_TYPE`/`stacksentry-menu`.

- Expanded stack registry: S-UI, Marzban, Marzneshin, PasarGuard, Hiddify,
  Remnawave, Outline; Xray, V2Ray, sing-box, Hysteria, TUIC, Trojan, NaiveProxy,
  Mieru, ShadowTLS, Brook, mtg, Shadowsocks, Squid, Dante, 3proxy, GOST;
  WireGuard, AmneziaWG, OpenVPN, IPsec, SoftEther, Tailscale, ZeroTier, NetBird,
  Headscale, Nebula. Detection by units, paths and processes; apk/xbps/emerge/
  eopkg/pkg packages.

- Read-only Mini App: a Telegram dashboard (overview, services, panel, traffic,
  subscriptions, history), initData login, HttpOnly cookie, CSP, the
  `stacksentry-webapp` service on 127.0.0.1:8787, and a “Panel” Telegram menu
  button. No actions and no secrets in the browser.

- Monitor-only: without the 3X-UI API the bot watches services, the VPN stack and
  hardware; `PANEL_URL`/`PANEL_API_TOKEN` may both be empty, the installer detects
  3X-UI and asks whether to configure the API. `--no-panel`/`--panel`.

- TOTP 2FA for dangerous bot actions: step-up means read-only without a code,
  restarts and apt update after a code with a 15-minute unlock, OS upgrades and
  reboot require a fresh code. Codes are one-time, 3 attempts, 5-minute lockout,
  audited; the code message is deleted. The installer asks and prints otpauth;
  `sudo stacksentry-menu totp` shows the secret.

- Separate, self-reporting updates: `apt update`, `full-upgrade`, `phased`,
  release check (`do-release-upgrade --check`) and the actual
  `do-release-upgrade`. The bot watches the systemd unit and sends the exit code
  and log tail; pending jobs survive a bot restart (SQLite).
- A server command can be sent from any screen (or `/run <command>`): validated
  against the allowlist, confirmed, output returned.
- New `/traffic` command (previously only a button).
- Menus no longer pile up: moving to a new screen replaces the previous one
  (Details and Collapse expand the same screen).
- `/help` is now a full manual: sections, updates, commands, metrics, security,
  troubleshooting and settings.
- `stacksentry-menu apt-logs [N]` and a README section explaining every menu item, what to
  enter and an example value.
- Updating through `stacksentry-menu` reinstalls code, wrapper, menu, units and sudoers,
  so new features apply on future updates too.

- Monitoring bot: system metrics, systemd services, 3X-UI panel, xray, inbounds,
  subscriptions and traffic; alerts with hysteresis, cooldown and an “I know,
  until morning” mute stored in SQLite.
- Confirmed actions: service/xray restart, `apt` full and phased in a transient
  unit, release-upgrade check, server reboot, allow-listed command.
- In-bot manual (`/help` and the “❓ Help” button) explaining every metric.
- `stacksentry-menu` terminal panel: status, masked settings view, allow-listed editing,
  restart, log, update, removal.
- `stacksentry-menu` menu redrawn in a frame (like the 3X-UI panel): items grouped into
  sections, live service state on top, frame width follows the longest line, a
  wrong number prints the valid range. Added threshold submenus, services/log-level
  editing and an xray restart via the panel API (`stacksentry-menu restart-xray`).
- One-command GitHub install: the wizard asks for tokens as step 4 (before
  installing dependencies), verifies the bot token and panel reachability, can
  issue the panel token itself; an 8-check self-test at the end.
- systemd, OpenRC (Alpine, Gentoo) and SysVinit support with automatic service
  manager detection (`--init` to force) and an init script plus log file fallback
  for non-systemd hosts.

### Documentation

- README (ru/en) restructured into a logical chain: why → preview → compatibility
  → quick start → panel connection → internals → commands → `stacksentry-menu` → settings
  → security → update/removal → troubleshooting → development. Added a table of
  contents, compatibility and symptom tables, screen examples and exact API/path
  checks.
- CI now checks `stacksentry-menu` and the init script syntax, runs `shellcheck` on the
  menu, and validates the sudoers generated from real unit names and command keys.

### Added

- Optional terminal: separate `stacksentry-terminal` service, `stacksentry-term` user,
  Telegram initData + TOTP login, one-time tickets, access token for non-Telegram
  access, optional root via the exact sudoers rule `/bin/bash -l`. Listens on
  127.0.0.1 by default.
- Panel/VPN inventory: 3X-UI, S-UI, Marzban, Hiddify, Xray, sing-box, WireGuard,
  AmneziaWG, OpenVPN, Shadowsocks, Tailscale; the installer fills `SERVICES`,
  `/stack` shows detected vs watched.
- Package managers: apk (Alpine), xbps (Void), pkg (FreeBSD, best-effort).

### Security

- Terminal audit: secrets are no longer inherited by the PTY shell; the normal
  shell runs as `stacksentry-shell` without sudo, root uses a separate rule; TOTP
  is normalized for replay protection; fixed fd leak, session-map race and
  non-ASCII cookie/ticket handling.
- Installer: `--2fa`/`--no-2fa`/`--no-panel` work on a complete `.env`;
  monitor-only counts as a complete config; re-running `--terminal`/`--webapp`
  preserves secrets/origin/root; `GH_TOKEN` is sent only to github.com.
- Mini App: `SameSite=None+Secure` for Telegram and `Lax` for localhost, no
  infinite retry; monitor-only reboot report is honest.

### Fixed

- Logs from `/run` (`os_logs`, `xui_logs`, `kernel_logs`, `dmesg`) now have
  “📜 200 more lines” and “🔎 Errors only” buttons, like service logs. Log
  variants are allowed only when the base log is in `COMMAND_ALLOWLIST`.
- The safe-command list in the bot (`/run` without arguments and the “Run a
  command” button) is now grouped and described, not just aliases.
- After the `COMMAND_ALLOWLIST` migration `.env` is restored to
  `640 root:<service group>`: `set_env` under umask 077 left it `root:root 600`
  and the bot failed to start with `PermissionError: .env`. The service group is
  now resolved via `id -gn` instead of assuming it equals the user name.
- Traffic screen shows per-inbound totals and top clients by usage; when the
  panel returns nothing it explains what to check (scope, webBasePath, token).
- One action is one message: “queued” → “running” → report (previously both
  “Запущено в фоне” and “… запущено” piled up). Running titles fixed.
- Removed the duplicate release-check button: `do-release-upgrade` checks the
  version itself; “No new release found” (exit 1) is treated as success.
- Alerts no longer report “recovered” when a collector was not observed
  (`unknown` instead of `ok`); duplicate “system metrics unavailable” removed.
- Subscriptions: a client attached to several inbounds is no longer duplicated;
  traffic and quota are aggregated.
- Panel: `api_authorized` is reset between polls; a non-dict `xray` no longer
  breaks the check.
- System: a failing `DISK_PATH` or network does not wipe other metrics;
  `is_active` works on OpenRC/SysV.
- Menu: “Refresh/Menu” no longer strands a screen without a keyboard on throttle;
  long HTML messages are split with tags closed and reopened.
- Cooldown is per target (x-ui and fail2ban do not block each other), TTL and
  cooldown use `monotonic`; an action mismatch no longer burns the confirmation;
  unit names cannot start with `-`; `summary(0)` no longer returns everything;
  a failed xray restart is also rate-limited.
- The safe command set is expanded and documented: OS/kernel/x-ui logs, network,
  services, updates, hardware: read-only, no shell.

### Fixed (older)

- Clean-machine install: `/opt/stacksentry` is created before `.env` is
  written; an incomplete `.env` after an aborted run is completed by the wizard.
- Panel address handling: spaces/trailing slashes stripped, a path pasted with the
  address is moved into `webBasePath`, double slashes are impossible.
- `webBasePath` is now verified before the token is requested (a valid API path
  answers 401, a wrong one 404): the `/panel` browser path, which is not
  `webBasePath`, no longer produces a doubled `.../panel/panel/api` segment.
  A bad token (empty 401) is no longer reported as “panel did not respond”;
  401/403, 404 and unreachable are told apart in the wizard and the self-check.
- `asyncio.TimeoutError` on Python 3.10 (command timeouts did not fire); orphan
  processes on cancellation; `RetryAfter` no longer drops messages; cooldown
  checked under lock; confirmation tokens not consumed by a foreign tap.
- Uninstall now prints a one-line “Bot removed.” report instead of listing what
  and where was deleted (`--keep-state` still warns about the leftover database
  and user); `stacksentry-menu` exits after removal instead of re-opening the menu.
- The Maintenance section no longer “hangs”: on a command timeout `run_command`
  left its output variables unset and the job died with `UnboundLocalError` and
  no report. Output is now pre-initialised, and on timeout the whole process
  group is killed, so a grandchild (`do-release-upgrade` → `apt`) can no longer
  hold the pipes or survive. Any action error (not just `ActionError`) is now
  reported in the chat and audited.
- Release-upgrade check: `do-release-upgrade` wrote to the read-only
  `PREFIX/.cache` (ProtectSystem=strict) and failed with `mkdir() failed`; it now
  gets a writable `HOME`/`XDG_CACHE_HOME` under the state dir, and the check is
  capped at 120 s instead of `COMMAND_TIMEOUT`.
- System upgrade: `systemd-run` goes through polkit in a daemon context on new
  systemd (Ubuntu 26.04) and is denied (“interactive authentication required”),
  so the button silently did nothing. The installer now adds persistent
  `stacksentry-apt-upgrade.service` / `stacksentry-apt-phased.service`
  (`User=root`) units started via `systemctl`, with transient and detached
  fallbacks.

### Security

- The installer validates `--prefix` and `STATE_DIR`: `/`, `/etc`, `/usr`, `/var`
  and similar paths are rejected (`ALLOW_UNSAFE_PREFIX=1` to override). Previously
  `--prefix /etc` could run `chmod -R` and `rm -rf` on a system directory.
- With `--sha256`, the git fallback is disabled: unverified code is never installed.
- The init script no longer sources `.env` as shell code; `reboot` works on
  OpenRC/SysV; `STATE_DIR` is templated into the unit.
- The panel token probe uses a `mktemp` file instead of a predictable
  `/tmp/.xui-mon-bot-tok`.

- Secrets never reach logs (scrubbing formatter), Telegram IDs masked,
  `stacksentry-menu show/check` never print secrets.
- `PANEL_CA_BUNDLE` certificate pinning, `--sha256` archive verification, no
  redirect following for the panel client.
- Commands accepted only in the administrator's private chat, denials logged,
  narrow sudoers allowlist for `stacksentry-ctl`, `run_command` executes fixed argv
  without `bash -c`.
