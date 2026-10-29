# StackSentry

> VPS, panel and VPN monitoring in Telegram.

[![CI](https://github.com/Hijackery/stacksentry/actions/workflows/ci.yml/badge.svg)](https://github.com/Hijackery/stacksentry/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)
![Platform](https://img.shields.io/badge/platform-systemd%20%7C%20openrc%20%7C%20sysv-4EAA25)
![License](https://img.shields.io/badge/license-MIT-blue)

[Русский](README.md) · **English**

**StackSentry** is a Telegram bot for VPS: it watches hardware, services, panels
(3X-UI, Marzban, Marzneshin, PasarGuard, Remnawave, S-UI, Hiddify, Outline) and
VPN stacks (WireGuard, OpenVPN, AmneziaWG, sing-box, Xray, Shadowsocks,
Tailscale), sends alerts and shows subscriptions. Risky actions (restart, system
upgrade, reboot) happen only from a confirmed button and only for an
administrator.

Current version: **1.1.0**. The project is public:
[documentation](docs/README.md), [security policy](SECURITY.md). Updates come
from `main` via the in-bot “Check for updates” button or
`sudo stacksentry-menu update`; GitHub Releases and tags are not used.

One-command install:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh)
```

Alternatives without `curl | bash`:

```bash
# download and inspect the installer separately
curl -fsSLo /tmp/stacksentry-install.sh \
  https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh
sudo bash /tmp/stacksentry-install.sh

# GitHub CLI
gh api repos/Hijackery/stacksentry/contents/install.sh \
  -H "Accept: application/vnd.github.raw" > /tmp/stacksentry-install.sh
sudo bash /tmp/stacksentry-install.sh

# private fork or corporate GitHub with a token
GH_TOKEN=... bash -c 'curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
  https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh | bash'
```

> The wizard downloads the code, asks for tokens, installs dependencies, creates a
> dedicated user, enables autostart and verifies that everything works.

## Contents

1. [Why use it](#why-use-it)
2. [What it looks like](#what-it-looks-like)
3. [Compatibility](#compatibility)
4. [Quick start](#quick-start)
5. [Connecting to the panel](#connecting-to-the-panel)
6. [How it works](#how-it-works)
7. [Commands and screens](#commands-and-screens)
8. [`stacksentry-menu` settings menu](#stacksentry-menu-settings-menu)
9. [Settings](#settings)
10. [Security](#security)
11. [Update and removal](#update-and-removal)
12. [Troubleshooting](#troubleshooting)
13. [Documentation](#documentation)
14. [Versions and updates](#versions-and-updates)
15. [Development](#development)
16. [Project](#project)

---

## Why use it

Panels and VPN show their state, but you have to open them. The bot brings what
matters to Telegram and stays quiet when everything is fine.

| | |
|---|---|
| **Monitoring** | CPU, RAM, swap, disk, network, load; services; panel and core; panel/VPN inventory; users, subscriptions and traffic |
| **Panels** | API for 3X-UI, Marzban, Marzneshin, PasarGuard, Remnawave, S-UI, Hiddify, Outline; others detected as service/port/process |
| **Alerts** | hysteresis without flapping, cooldown, an “I know, until morning” mute; restart and log buttons right on the alert |
| **Actions** | service and panel core restart, package updates, OS release upgrade (APT), reboot, allow-listed commands |
| **Updates** | `main` check and an “Update StackSentry” button with confirmation and 2FA; no GitHub Releases or tags |
| **History** | hourly CPU/RAM/disk averages for 24 hours, 7 or 30 days; p95 and peaks |
| **Install** | one command, an adaptive wizard for OS/services/panel, unattended mode, self-check |
| **Universality** | systemd, OpenRC, SysVinit; `apt`/`dnf`/`yum`/`zypper`/`pacman`/`apk`/`xbps`/`emerge`/`eopkg`/`pkg`; Python 3.10+ |
| **Mini App** | read-only Telegram dashboard: overview, services, panel, traffic, subscriptions, history, terminal and built-in Help; Telegram login (+ TOTP when 2FA is enabled) |
| **Terminal** | `stacksentry-menu` handles settings, diagnostics, restart, log and update; secrets are masked |
| **Security** | a dedicated unprivileged user, a narrow sudoers list, confirmations and an audit log for risky actions |

## What it looks like

The summary starts with a verdict: one glance is enough at night:

```text
🟢 Server state: all good · CPU 12% · RAM 41% · Disk 63% · x-ui 🟢 · panel 🟢

CPU  ▓▓░░░░░░░░  12.0% 🟢
RAM  ▓▓▓▓░░░░░░  41.0% 🟢
Disk ▓▓▓▓▓▓░░░░  63.0% 🟢
```

An alert with a reason and actions:

```text
🔴 CRITICAL · Service x-ui is down · 03:12
Status failed, SubState failed

[♻️ Restart x-ui] [📜 x-ui log]
[📊 Current state] [🏠 Menu]
[😴 I know, until morning]
```

An action result with the return code and a contextual button:

```text
✅ x-ui restarted in 1.2 s
Service x-ui restarted
[📜 x-ui log] [🏠 Menu]
```

## Compatibility

| Item | Support |
|---|---|
| **Services** | systemd (Debian/Ubuntu, RHEL/Rocky/Alma, Fedora, openSUSE, Arch), OpenRC (Alpine, Gentoo), SysVinit |
| **Packages** | `apt`, `dnf`, `yum`, `zypper`, `pacman`, `apk`, `xbps`, `emerge`, `eopkg`, `pkg` are detected automatically |
| **Python** | 3.10+; the installer picks a suitable interpreter |
| **Panels** | API: 3X-UI, Marzban, Marzneshin, PasarGuard, Remnawave, S-UI, Hiddify, Outline. Other VPN/proxy stacks are detected as service/port/process |
| **Core restart** | 3X-UI, Marzban, Marzneshin, PasarGuard, S-UI via API; Remnawave/Hiddify/Outline via service/container only |
| **sudo** | classic sudo and `sudo-rs` (Ubuntu 26.04+): rules are generated without wildcards or aliases |
| **Architectures** | x86_64, ARM, and anything else with Python and a supported init system |

You can pick the service manager explicitly with `--init systemd|openrc|sysv`.
Without systemd the bot still works: the log is read from `bot.log`, and service
state is reported as unavailable (no false “crit” alerts).

## Quick start

### 1. What you need

- a Linux server with the 3X-UI panel;
- a Telegram bot token from [@BotFather](https://t.me/BotFather);
- your numeric ID (e.g. [@userinfobot](https://t.me/userinfobot));
- a panel API token with the `node-sync` or `admin` scope. See the
  [next section](#connecting-to-the-panel).

### 2. Install

One-command install:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh)
```

Alternatives without `curl | bash`:

```bash
# download and inspect the installer separately
curl -fsSLo /tmp/stacksentry-install.sh \
  https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh
sudo bash /tmp/stacksentry-install.sh

# GitHub CLI
gh api repos/Hijackery/stacksentry/contents/install.sh \
  -H "Accept: application/vnd.github.raw" > /tmp/stacksentry-install.sh
sudo bash /tmp/stacksentry-install.sh

# private fork or corporate GitHub with a token
GH_TOKEN=... bash -c 'curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
  https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh | bash'
```

Or through a pipe (this form works with `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh | sudo bash
```

> Do not add `sudo` to `bash <(curl …)`: sudo closes the process-substitution
> file descriptor and bash prints
> `bash: /dev/fd/63: No such file or directory`. Either run `bash <(…)` as root
> without sudo, or use the pipe form `curl … | sudo bash`.

The wizard asks questions as **step 4, before installing dependencies**, so
answer them at the beginning. Questions are read from the terminal (`/dev/tty`),
so the dialogue works even when the script is piped.

What the wizard asks:

1. the bot token and admin IDs (the token is verified with Telegram immediately);
2. 2FA (TOTP) is optional: when enabled, the code is required for Mini App login and dangerous actions; when declined, TOTP is not used;
3. the panel address, `webBasePath` and API token (the address is verified);
4. the services to watch;
5. an optional alert chat;
6. the Mini App gets HTTPS automatically (`sslip.io` + Caddy); for the terminal
   you can specify your own address or keep the SSH tunnel.

The Mini App, terminal, root session and 2FA are installed by this same command;
a separate `install.sh --terminal ...` after installation is not needed.

### 3. After installation

```bash
sudo stacksentry-menu          # menu: state, settings, restart, log, update
systemctl status stacksentry-bot
```

Open the bot in Telegram, press `/start` → `/status`. If something is wrong, the
bot tells you what exactly. `/version` shows the version, OS, package manager and
selected panel; `/stack` shows what was found on the server. The Mini App and
terminal are already installed: if you provided public HTTPS addresses during
installation, a “Panel” button appears next to the attachment icon, and the main
menu gets an inline “📱 Open Mini App” button and a “Terminal” tab. Only one
manual step remains: bind the domain once via `@BotFather` → `/setdomain`
(without `https://`). This is a Telegram requirement; there is no API for it.

### Unattended install

For automation and re-installs: `--yes` asks no questions and takes values from
the environment or an existing `.env`. A complete `.env` is kept as is; you can
provide the missing values in advance. Enable the full set explicitly in
unattended mode:
`--webapp --webapp-origin https://... --terminal --root-terminal --terminal-origin https://...`.

```bash
sudo env \
  TELEGRAM_BOT_TOKEN='123456789:AA...' \
  TELEGRAM_ADMIN_IDS='11111111' \
  PANEL_TYPE='3x-ui' \
  PANEL_URL='https://panel.example.com:2053' \
  PANEL_API_TOKEN='...' \
  bash install.sh --yes
```

For Marzban/Marzneshin/PasarGuard pass `PANEL_USERNAME` and `PANEL_PASSWORD`
instead of a token; for Outline pass only `PANEL_URL` with the secret path.
`PANEL_TYPE` may be omitted; the wizard uses the detected type or `3x-ui`. The
full type/auth matrix is in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

Useful options: `--repo owner/name`, `--ref branch`, `--prefix DIR`, `--user NAME`,
`--no-start`, `--force-config`, `--print-source`, `--sha256 HASH`, `--init TYPE`,
`--webapp --webapp-origin https://...`, `--terminal`, `--root-terminal`, `--2fa`,
`--menu`. Full list: `bash install.sh --help`.

Re-running the installer updates the code and keeps `.env`/state; to check for
updates from the bot use `/updatecheck`, the Help button and the Mini App Help tab.

## Connecting to the panel

### Panel APIs

| Panel | Auth | What it gives |
|---|---|---|
| 3X-UI 2.x/3.x | Bearer API token | status, inbounds, subscriptions, traffic, xray restart |
| Marzban | `PANEL_USERNAME` + `PANEL_PASSWORD` (OAuth2) | status, users, traffic, xray restart |
| Remnawave | Bearer API token | status, users, traffic |
| Marzneshin | username/password (OAuth2 `/api/admins/token`) | status, users, traffic, backend restart by re-applying the current config |
| PasarGuard | username/password (OAuth2 `/api/admin/token`) | status, users, traffic, core restart |
| S-UI | `PANEL_API_TOKEN` in the `Token` header | status, clients, traffic, sing-box restart |
| Hiddify | `PANEL_API_TOKEN` (admin UUID) in `Hiddify-API-Key` | status, users, traffic |
| Outline | secret `SB_API_PREFIX` path in `PANEL_URL` | status, access keys, traffic |

Switch with `sudo stacksentry-menu set PANEL_TYPE hiddify` (plus URL and token),
then restart. Use `PANEL_API_TOKEN` for 3X-UI/Remnawave/S-UI/Hiddify,
username/password for Marzban/Marzneshin/PasarGuard, and only the secret path in
`PANEL_URL` for Outline.

URL notes:
- S-UI: `PANEL_URL` is the panel root including its web path, e.g.
  `http://host:2095/app`; the bot appends `/apiv2/`.
- Hiddify: `PANEL_URL` is the root including the admin proxy path, e.g.
  `https://example.com/AbCdEf`; the bot appends `/api/v2/`.
- Outline: `PANEL_URL` is the full address including the secret
  `SB_API_PREFIX`, e.g. `https://host:port/SecretPath`; no separate token.
- 3X-UI: `PANEL_BASE_PATH` is the panel webBasePath; the bot appends
  `/panel/api`.

The installer, menu and bot adapt to the server. The wizard detects the OS,
package manager, services and panel type, then asks only for the relevant
credentials: a token for 3X-UI/Remnawave/S-UI/Hiddify, username/password for
Marzban/Marzneshin/PasarGuard, or a secret path for Outline, then probes the API.
`stacksentry-menu detect` shows what was found, the menu has a panel type item,
and the core restart picks the right endpoint. The bot hides
Traffic/Subscriptions until an API is configured and shows update actions for the
detected package manager (APT-specific actions only on APT).

### What is detected

Panels: 3X-UI, S-UI, Marzban, Marzneshin, PasarGuard, Hiddify, Remnawave,
Outline. Proxies/cores: Xray, V2Ray, sing-box, Hysteria, TUIC, Trojan,
NaiveProxy, Mieru, ShadowTLS, Brook, mtg, Shadowsocks, Squid, Dante, 3proxy,
GOST. VPN/mesh: WireGuard, AmneziaWG, OpenVPN, IPsec/StrongSwan, SoftEther,
Tailscale, ZeroTier, NetBird, Headscale, Nebula.

Detection uses units (systemd/OpenRC/SysV), config paths and process names
(`ps`). API integration exists for 3X-UI (inbounds, subscriptions, xray restart),
Marzban, Marzneshin, PasarGuard, Remnawave, S-UI, Hiddify and Outline
(users/clients, traffic); the rest are shown as service/port and marked
“API not supported”.
Docker containers are visible as the `docker` service but are not queried via the
Docker API (that would give the bot root-equivalent rights).

### No panel: monitor-only

The installer detects panels/VPN by services, paths and ports. If no supported
panel is found, leave `PANEL_URL` and `PANEL_API_TOKEN` empty, and the bot then monitors
services, the VPN stack, hardware and alerts, while panel/subscription screens
say the API is not configured. Enable it later via `sudo stacksentry-menu`
(panel address and token) or reinstall with `--panel`.

### 3X-UI token

The token is created **inside the panel**, not in Telegram and not in the bot.

**3X-UI v3.x**

1. “Panel settings → Account → API token” → create.
2. Scope: **`node-sync`** (or `admin`).
3. **Copy the value immediately**: the panel shows it once, later only a
   “configured” marker remains.

The same via API: `POST /panel/api/setting/apiTokens/create` with `name`, `scope`,
`expiresAt` (an existing admin token is required).

**3X-UI 2.x**

“Settings → API Tokens”, or from the terminal:

```bash
sudo x-ui setting -getApiToken true -tokenName monitor-bot -tokenScope node-sync
```

On v3 this command no longer creates tokens; it only rotates the service
`cli-fallback` one.

Why not `monitor`: that scope only exposes panel status. Inbounds and
subscriptions need `node-sync`; actions inside the panel need `admin`.

### `webBasePath`: a common mistake

`webBasePath` is the separate **“Panel URL”** setting in 3X-UI. It is usually `/`,
and then the wizard field is left empty.

Do not enter the `/panel` path from the browser: the 3X-UI UI always lives at
`.../panel`, and the API at `.../panel/api`. Putting `/panel` into `webBasePath`
adds a duplicate segment (`.../panel/panel/api`).

The installer checks the path before requesting the token (a valid path answers
`401` without a token, a wrong one `404`) and silently fixes the mistake. A full
address pasted into that field is recognised too.

### Manual check

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'X-Requested-With: XMLHttpRequest' \
  https://panel.example.com:2053/panel/api/server/status
```

`401` means the path is correct and a token is needed. With the token:

```bash
curl -fsS -H "Authorization: Bearer <token>" \
  https://panel.example.com:2053/panel/api/server/status
```

The response must contain `"success":true`.

### Address: IP, domain, localhost

- If the panel has **“Panel domain”** (`webDomain`) configured, 3X-UI checks the
  `Host` header and answers `403` to everything else. Then `PANEL_URL` must use
  exactly that name, not the IP.
- The name must resolve **from the server itself**. If the domain points to a
  public IP and there is no hairpin NAT, use `http://127.0.0.1:2053` with the same
  `PANEL_BASE_PATH`. Write IPv6 in brackets: `http://[::1]:2053`.
- `http` is fine when the panel listens on localhost: traffic never leaves the
  machine. If the panel is reachable publicly, use `https://` only, otherwise the
  token and API contents travel in plain text. For a self-signed certificate, do
  not disable verification; set `PANEL_CA_BUNDLE` instead.
- The bot does not follow redirects: if `http` redirects to `https`, change
  the scheme.

## How it works

```text
Telegram / Mini App  ⇄  bot (unprivileged user stacksentry)
                          ├─ psutil / systemctl / journalctl: metrics and services
                          ├─ inventory: OS, packages, panels, VPN
                          ├─ panel API (8 types): users, traffic, core restart
                          ├─ updates: commit check against main
                          └─ sudo stacksentry-ctl (narrow sudoers): restart, updates, reboot, journal
```

- **Code**: `/opt/stacksentry`, owned by `root`; the bot cannot rewrite itself.
- **Secrets**: `.env` with `640 root:stacksentry`.
- **State**: SQLite in `/var/lib/stacksentry` (history, audit, mutes).
- **Privileges**: only through `sudo stacksentry-ctl`; the allowed subcommands are listed
  exactly, without wildcards.
- **Updates** run in dedicated systemd units (`stacksentry-apt-*.service`)
  because the bot lives with `ProtectSystem=strict`; `stacksentry-ctl` detects
  `apt`/`dnf`/`yum`/`zypper`/`pacman`/`apk`/`xbps`/`emerge`/`eopkg`/`pkg`.
- **Self-update**: the installer runs in a separate unit from `main`, keeps
  `.env`/state and restarts the bot.
- **Mini App**: a separate read-only service on `127.0.0.1:8787`; expose it only
  via an HTTPS proxy; login is via Telegram initData.

## Commands and screens

| Command | What it shows |
|---|---|
| `/status` | a one-line verdict, issues, metrics, services and panel |
| `/traffic` | per-inbound totals and top clients by traffic |
| `/services` | service state; restart and log next to each |
| `/panel` | panel and xray availability, versions, updates |
| `/subs` | subscriptions: expiry, traffic, depleted and disabled |
| `/history` | hourly CPU/RAM/disk averages for 24 hours, 7 or 30 days |
| `/logs` | selected service log with an “errors only” filter |
| `/audit` | bot action log with “denied / actions” filters |
| `/maint` | maintenance: `apt`, release upgrade, reboot, allowlisted command |
| `/run <command>` | run a diagnostic command from the allowlist |
| `/help` | plain-language help: what to press and what the numbers mean |

Buttons duplicate the commands; every screen has “🏠 Menu”, so you cannot get
stuck. Guests do not see the buttons. Moving between menus replaces the previous
screen, so no chain of messages piles up (Details and Collapse are the exception:
they expand the same screen).

**A server command can be sent from any section**. Just send it as a message (or
`/run <command>`). The bot asks for confirmation and returns the output. Only safe
diagnostic commands from `COMMAND_ALLOWLIST` are allowed; `/run` without arguments
lists them. There is no arbitrary shell.

### Safe diagnostic commands

`/run <command>` (or send it as a message) executes only commands from
`COMMAND_ALLOWLIST`. All of them are **read-only** and run as argv without a
shell: no pipes, redirects or writes. If a command is not installed, the bot says
so explicitly.

| Command | What it shows |
|---|---|
| `os_logs` / `os_errors` | OS journal: warnings / errors only (100 lines) |
| `kernel_logs` / `dmesg` | kernel messages |
| `xui_logs` | the `x-ui` service journal |
| `xui_service` / `x-ui status` | panel status |
| `failed_units` | failed systemd units |
| `sockets` / `net_summary` | listening ports / socket summary |
| `addresses` / `routes` | IP addresses / routes |
| `top_cpu` | processes by CPU and memory |
| `uname` / `cpu_info` / `block_devices` | system, CPU, disks |
| `log_size` / `inodes` / `df -h` / `free -h` / `uptime` | space and resources |
| `upgradable` / `upgrade_sim` | available updates / `apt` simulation |
| `vmstat` / `time_status` / `logged_in` / `boot_time` | load, time, sessions |
| `journalctl --disk-usage` / `systemctl list-timers --all` | journal and timers |

Examples: `/run os_logs`, `/run failed_units`, `/run sockets`. `/run` without
arguments shows this list right in the bot, grouped and described. For logs
(`os_logs`, `xui_logs`, `kernel_logs`, `dmesg`) the output has “📜 200 more lines”
and “🔎 Errors only” buttons, just like service logs.

**Commands and buttons work only in the administrator's private chat.** In a group
the bot refuses: addresses, subscriptions and traffic must not leak into a shared
chat. `ALERT_CHAT_ID` allows *outgoing* alerts to a group only; management there
is unavailable.

**Maintenance actions are separate and report to the chat.** `apt update` refreshes
indexes and lists the packages available for upgrade; `full-upgrade` installs everything; `phased` allows Ubuntu phased
updates; “run release” runs `do-release-upgrade`, which first checks for a new OS
version and says so if there is none. These are different things: `apt update`
does not check for a new OS version. The bot watches the background systemd unit
and sends the exit code plus the log tail when it finishes, so no `journalctl`
needed. One action is one message: “queued” → “running” → report, no pile-up. To
look manually: `sudo stacksentry-menu apt-logs`.

`/status` starts with a verdict; details are hidden behind “📋 Details”. Metrics
are aligned in a monospace block, and time is labelled “server time”. Alerts carry
`♻️ Restart`, `📜 Log` and `😴 I know, until morning`, an 8-hour mute stored in
SQLite that survives restarts.

## `stacksentry-menu` settings menu

A console panel is available after installation:

```bash
sudo stacksentry-menu              # interactive menu
sudo stacksentry-menu show         # settings (secrets masked)
sudo stacksentry-menu keys         # which keys may be changed
stacksentry-menu check PANEL_TIMEOUT 20   # validate without changes (no root)
sudo stacksentry-menu set PANEL_TIMEOUT 20
sudo stacksentry-menu restart       # apply
sudo stacksentry-menu detect        # detected OS, panels, VPN, services
sudo stacksentry-menu restart-xray  # restart the core when supported
sudo stacksentry-menu apt-logs 100  # log of background apt upgrades
sudo stacksentry-menu logs 100
```

The interactive menu is drawn in a frame and grouped into sections; the live
service state is shown on top:

```text
╔════════════════════════════════════════════════════════╗
│              StackSentry: control panel               │
│  Service stacksentry-bot: ● running                    │
│   Diagnostics                                          │
│    1. Service state and recent errors                  │
│    2. Show settings (secrets masked)                   │
│    3. Bot log (last 50 lines)                          │
│    4. Detected OS, panels, VPN, services               │
│────────────────────────────────────────────────────────│
│   Telegram                                             │
│    5. Bot token (@BotFather)                           │
│    6. Admin IDs                                        │
│    7. Alert chat                                       │
│   …                                                    │
│   25. Check for StackSentry updates                    │
│   26. Remove the bot                                   │
│    0. Exit                                             │
╚════════════════════════════════════════════════════════╝
```

### What each item does and what to enter

| Item | What it does | What to enter | Example |
|---|---|---|---|
| 1 | service state and recent errors | - | - |
| 2 | shows settings, secrets masked | - | - |
| 3 | bot log, 50 lines | - | - |
| 4 | auto-detect: OS, package manager, panels, VPN, services | - | - |
| 5 | Telegram bot token | the value from @BotFather | `123456789:AAH...` |
| 6 | administrator IDs | numbers separated by commas | `11111111,22222222` |
| 7 | alert chat | chat ID; group IDs are negative; empty: private | `-1001234567890` |
| 8 | panel type (`PANEL_TYPE`) | one of the supported types | `s-ui` |
| 9 | panel address (`PANEL_URL`) | scheme, host, port and (S-UI/Hiddify) the panel path | `https://panel.example.com:5580` |
| 10 | panel path (`PANEL_BASE_PATH`) | 3X-UI `webBasePath`; S-UI/Hiddify usually include it in the URL | empty or `mypanel` |
| 11 | panel API token | 3X-UI/Remnawave/S-UI token or Hiddify admin UUID | `AbCdEf...` |
| 12 | Marzban username and password (submenu) | `PANEL_USERNAME`, `PANEL_PASSWORD` | `admin`, `***` |
| 13 | TLS and CA (submenu) | `PANEL_VERIFY_TLS`: `true`/`false`; `PANEL_CA_BUNDLE`: PEM path | `true`, `/etc/ssl/panel-ca.pem` |
| 14 | CPU thresholds (submenu) | `CPU_WARN` and `CPU_CRIT`, % | `80`, `95` |
| 15 | memory thresholds (submenu) | `MEM_WARN` and `MEM_CRIT`, % | `85`, `95` |
| 16 | disk thresholds (submenu) | `DISK_WARN` and `DISK_CRIT`, % | `85`, `93` |
| 17 | hysteresis and cooldown (submenu) | `ALERT_HYSTERESIS` in %, `ALERT_COOLDOWN` in s | `5`, `1800` |
| 18 | days before a subscription expiry warning | a whole number of days | `3` |
| 19 | monitored services | service names separated by commas | `x-ui,fail2ban` |
| 20 | log level | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` |
| 21 | any allow-listed key | the key name first, then the value | `DISK_PATH` → `/` |
| 22 | restart the bot and verify | - | - |
| 23 | restart the panel core | `y` to confirm | `y` |
| 24 | update from `main` | - | - |
| 25 | check for StackSentry updates | - | - |
| 26 | removal | `yes` to confirm | `yes` |

An empty value clears a setting: `sudo stacksentry-menu set PANEL_BASE_PATH ""`.
For items 4–17 you can skip the menu and use
`sudo stacksentry-menu set KEY VALUE` directly (same as item 18, one command).

```bash
# value examples
sudo stacksentry-menu set PANEL_URL https://panel.example.com:5580
sudo stacksentry-menu set PANEL_BASE_PATH ""          # “Panel URL” = /
sudo stacksentry-menu set TELEGRAM_ADMIN_IDS 11111111,22222222
sudo stacksentry-menu set ALERT_CHAT_ID -1001234567890
sudo stacksentry-menu set SERVICES x-ui,fail2ban
sudo stacksentry-menu set CPU_WARN 80
sudo stacksentry-menu set LOG_LEVEL INFO

# validate a value without changing anything
stacksentry-menu check CPU_WARN 80

# check for StackSentry updates and view the package update log
sudo stacksentry-menu update-check
sudo stacksentry-menu apt-logs 100
```

Worth knowing:

- input is validated: a wrong number prints the valid range, `q` exits;
- only allow-listed keys can be changed (`stacksentry-menu keys`); values are validated
  before writing;
- `.env` is rewritten atomically with `640 root:<service user>`;
- secrets are never printed in full;
- paths can be overridden: `sudo PREFIX=/opt/bot ENV_FILE=/opt/bot/.env stacksentry-menu`;
- screen clearing can be disabled: `STACKSENTRY_NO_CLEAR=1 sudo stacksentry-menu`.

## Settings

All parameters live in `/opt/stacksentry/.env`; a commented example is in
[`.env.example`](.env.example). Process environment variables take precedence
over the file.

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | bot token from @BotFather |
| `TELEGRAM_ADMIN_IDS` | administrator IDs, comma-separated |
| `ALERT_CHAT_ID` | alert chat (empty: admins in private) |
| `TELEGRAM_API_URL` | custom Bot API or proxy (official by default) |
| `PANEL_URL` | panel address, e.g. `https://panel.example.com:2053` |
| `PANEL_BASE_PATH` | “Panel URL” (`webBasePath`), if set |
| `PANEL_API_TOKEN` | panel token (3X-UI `node-sync`, Remnawave, S-UI `Token`, Hiddify admin UUID) |
| `PANEL_TYPE` | `3x-ui`, `marzban`, `marzneshin`, `pasarguard`, `remnawave`, `s-ui`, `hiddify` or `outline` |
| `PANEL_USERNAME` | Marzban username (`PANEL_TYPE=marzban` only) |
| `PANEL_PASSWORD` | Marzban password (`PANEL_TYPE=marzban` only) |
| `PANEL_VERIFY_TLS` | verify the panel TLS certificate |
| `PANEL_CA_BUNDLE` | custom CA file (PEM); overrides `PANEL_VERIFY_TLS` |
| `PANEL_TIMEOUT` | panel request timeout, seconds |
| `SERVICES` | services to watch, comma-separated (`x-ui,fail2ban`) |
| `DISK_PATH` | path to check for free space |
| `CPU_WARN` | CPU warning threshold, % |
| `CPU_CRIT` | CPU critical threshold, % |
| `MEM_WARN` | memory warning threshold, % |
| `MEM_CRIT` | memory critical threshold, % |
| `DISK_WARN` | disk warning threshold, % |
| `DISK_CRIT` | disk critical threshold, % |
| `INODE_WARN` | inode warning threshold, % |
| `INODE_CRIT` | inode critical threshold, % |
| `CHECK_FAILED_UNITS` | `1`: check failed systemd units |
| `CHECK_TLS` | `1`: check the panel TLS certificate expiry |
| `CHECK_BACKUP_PATH` | backup file or directory to check |
| `CHECK_BACKUP_MAX_AGE_H` | maximum backup age, hours |
| `CHECK_HTTP_URLS` | external HTTP URLs, comma-separated |
| `CHECK_DNS_HOSTS` | DNS names, comma-separated |
| `CHECK_SMART` | `1`: check SMART via smartctl (optional) |
| `ALERT_HYSTERESIS` | how far a metric must recover before an alert clears, % |
| `ALERT_CONFIRM_CHECKS` | consecutive checks needed to confirm an alert |
| `ALERT_RECOVERY_CHECKS` | consecutive checks needed to confirm recovery |
| `ALERT_COOLDOWN` | how often an active alert repeats, seconds |
| `ALERT_CRITICAL_CHAT_ID` | separate chat for critical |
| `ALERT_CRITICAL_TOPIC_ID` | topic in the critical chat |
| `ALERT_WARNING_CHAT_ID` | separate chat for warning |
| `ALERT_WARNING_TOPIC_ID` | topic in the warning chat |
| `ALERT_QUIET_START` | quiet hours start, `HH:MM` |
| `ALERT_QUIET_END` | quiet hours end, `HH:MM` |
| `EXPIRING_SOON_DAYS` | days before expiry when a subscription is “soon” |
| `CHECK_INTERVAL` | background check period, seconds (minimum 15) |
| `UPDATE_NOTIFY` | `1`: notify admins about a new StackSentry commit in main |
| `UPDATE_CHECK_INTERVAL` | background update check period, seconds (minimum 3600) |
| `ACTION_COOLDOWN` | pause between identical actions, seconds |
| `CONFIRM_TTL` | confirmation button lifetime, seconds |
| `TOTP_SECRET` | TOTP secret (base32) for 2FA; empty: disabled |
| `TOTP_REQUIRED` | `1`: require a code for Mini App login and dangerous bot actions |
| `ELEVATION_TTL` | how long an unlock lasts, seconds |
| `TOTP_MAX_ATTEMPTS` | code attempts before lockout |
| `TOTP_LOCKOUT` | lockout duration after too many attempts, seconds |
| `WEBAPP_ENABLED` | `1`: enable the read-only Mini App |
| `WEBAPP_HOST` | listen address (default `127.0.0.1`) |
| `WEBAPP_PORT` | Mini App port (default 8787) |
| `WEBAPP_PUBLIC_ORIGIN` | public HTTPS URL for the Telegram menu button |
| `WEBAPP_TERMINAL_ORIGIN` | public HTTPS terminal URL for the tab inside the Mini App |
| `WEBAPP_SECRET` | cookie signing secret (≥ 32 chars) |
| `WEBAPP_SESSION_TTL` | Mini App session lifetime, seconds |
| `WEBAPP_METRICS_TOKEN` | Bearer token for `/metrics` (empty: public) |
| `COMMAND_TIMEOUT` | allowlisted command timeout, seconds |
| `COMMAND_ALLOWLIST` | safe diagnostic commands (aliases, see above) |
| `STATE_PATH` | SQLite database |
| `LOG_PATH` | log file |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_JSON` | `1`: emit one-line JSON logs for journald/aggregators |
| `SUDO_WRAPPER` | path to `stacksentry-ctl` |

### Where alerts go

Leave `ALERT_CHAT_ID` empty and alerts go to every ID in `TELEGRAM_ADMIN_IDS` in
private. For a shared chat, add the bot there, send a message and find the chat ID
(`@userinfobot`, `@getidsbot`); group IDs are negative, e.g. `-1001234567890`.

Remember: alerts contain addresses, subscriptions and traffic: everyone in the
group will see them. Commands and buttons still work only in private chat.

### Mini App (optional, read-only)

A dashboard in Telegram: overview, services, panel, traffic, subscriptions,
history, updates, terminal (when enabled) and a Help tab with a short guide to
every section, 2FA and the terminal.

Cards and list rows are clickable: a service opens its state and journal, a
metric opens 24h/7d/30d history with p95 and peaks, a check shows the cause and
what to do, and a subscription opens the client and traffic. The header has a
single search across sections, services, and subscriptions.

Updates can be requested from the Updates tab. The request goes to a shared queue,
the bot asks for confirmation in Telegram, and 2FA requires a fresh code. The
webapp then shows stages and progress, surviving a webapp restart.

```bash
# fully automatic: public IP -> <ip>.sslip.io -> Caddy -> Let's Encrypt
sudo bash install.sh --webapp --no-terminal

# your own domain and an existing TLS proxy
sudo bash install.sh --webapp --webapp-origin https://sentry.example.com
```

A domain is not required for a normal install: the installer detects the public
IPv4, creates a free host such as `203-0-113-5.sslip.io`, installs Caddy and
obtains a Let's Encrypt certificate. It needs free ports `80` and `443` (if
`443` is taken, for example by Xray, it uses `8443`) and a reachable public IP.
Disable this with `--no-webapp-auto`.

During an interactive install the wizard shows the public IP, the future address
and any warnings, then offers three choices:

1. **Automatic**: `sslip.io` + Caddy (recommended).
2. **Your own domain**: enter a ready HTTPS URL with a valid certificate.
3. **Local only**: no public address, access through an SSH tunnel.

If the automatic option fails, the installer offers to retry, enter a custom
domain, or keep the Mini App local.

If the terminal is installed, the installer asks whether to open it inside the
Mini App. With automatic HTTPS it creates a separate `term.<host>` address and
the “Terminal” tab appears without any manual Caddy setup. For a custom domain
you can provide a ready HTTPS terminal URL. Terminal login is always protected
by Telegram and TOTP. If you do not need a public terminal, answer `n`, and access
stays available only through an SSH tunnel.

One manual step remains: bind the **domain** to the bot in `@BotFather` →
`/setdomain` → for example, `203-0-113-5.sslip.io` (without `https://` and
without the port). Then restart the bot:
`sudo systemctl restart stacksentry-bot`. If Caddy is unavailable in the
repositories or the ports are busy, the installer shows the reason and offers
another option.

- login via Telegram `initData`, no passwords; administrators only. If 2FA is
  enabled, an additional TOTP code is required for each new session; if disabled,
  the Mini App never asks for a code;
- `HttpOnly` cookie, 1-hour TTL; public HTTPS uses `Secure; SameSite=None` (Telegram Web), local access uses `SameSite=Lax`; CSP without third-party scripts;
- `stacksentry-webapp` listens on `127.0.0.1:8787`; expose it only via Caddy/Nginx
  with TLS; without a public origin use
  `ssh -L 8787:127.0.0.1:8787 root@server` → `http://127.0.0.1:8787`;
- the buttons appear only with `WEBAPP_ENABLED=1` **and** a non-empty
  `WEBAPP_PUBLIC_ORIGIN`; the installer sets both automatically. Without a public
  origin the Mini App is available only through an SSH tunnel, and there are no
  Telegram buttons;
- under these conditions the bot adds a “Panel” chat menu button (Mini App) and
  an inline “📱 Open Mini App” button to the administrator's main menu. “Panel”
  is the menu next to the attachment icon; the inline button lives in the bot
  menu itself and is shown only to IDs from `TELEGRAM_ADMIN_IDS`;
- if the panel API is not configured, the dashboard shows services/VPN/hardware
  and says inbounds/subscriptions are unavailable.

### Terminal (installed by the normal setup)

A full browser shell is a separate service, openable standalone or embedded as a
tab inside the Mini App. The normal interactive install already sets it up with
the root session and the Mini App tab; a separate command is only needed for
reconfiguration or automation:

```bash
sudo bash install.sh --terminal              # terminal only, no root
sudo bash install.sh --root-terminal         # + root session (sudo -i)
sudo bash install.sh --terminal-auto         # publish inside the Mini App
sudo bash install.sh --terminal --terminal-origin https://term.example.com
```

What happens:

- a dedicated `stacksentry-term` user and `stacksentry-terminal` service are created;
- the service listens on `127.0.0.1:8788` and is not exposed publicly;
- login: Telegram `initData` **and a mandatory TOTP code** for every session.
  Even if the administrator's Telegram account is compromised, access without
  TOTP is impossible; outside Telegram the access token from
  `/etc/stacksentry/terminal-secrets.env` (`600 root`) is also required;
- the installer automatically creates an Ed25519 SSH key
  (`/var/lib/stacksentry-term/terminal-ssh/id_ed25519`), adds its public part to the
  `authorized_keys` of `stacksentry-term`, and uses the key for a local SSH
  connection to `127.0.0.1`. The private key never leaves the server; if
  sshd/the key are unavailable, the terminal automatically falls back to a local PTY;
- terminal secrets live in `/etc/stacksentry/terminal-secrets.env` (`600 root`);
  systemd injects them, the service user never reads the file;
- the normal shell runs as a separate `stacksentry-shell` user **without sudo**;
  a root session uses only the exact
  `stacksentry-term ALL=(root) NOPASSWD: /bin/bash -l` rule. It is enabled in the
  normal setup; disable it with `--no-root-terminal`;
- terminal secrets (TOTP, tickets, access token, bot token) are **not passed**
  to the PTY: the shell gets only PATH/TERM/LANG/HOME/USER;
- the “Terminal” tab inside the Mini App appears when `WEBAPP_TERMINAL_ORIGIN`
  is set. With automatic HTTPS the installer publishes the terminal at
  `term.<host>` and writes the address; for a custom domain use
  `--terminal-origin` or answer the wizard. The terminal opens in an iframe,
  initData is passed via the hash, but TOTP is still requested separately.

Need only the bot without the extra services? Disable them at install time:
`--no-webapp`, `--no-terminal`, `--no-root-terminal`, `--no-2fa`.

Default access is local/SSH tunnel only:

```bash
ssh -L 8788:127.0.0.1:8788 root@server
# then open http://127.0.0.1:8788
```

For Telegram access set `--terminal-origin https://...` and put Caddy/Nginx with
TLS in front of `127.0.0.1:8788`.

> A root terminal is a real root shell. TOTP protects the login, but a compromised
> web layer/session/dependency defeats it. Keep it behind VPN/SSH and do not
> expose it publicly without a second perimeter.

## 2FA: Mini App and dangerous actions

When 2FA is enabled, the Mini App requires a TOTP code at login. In the bot, viewing state, logs and
subscriptions still needs **no code**; a code is required only where the server
can be broken (step-up). Otherwise you would have to re-enter a code every 15
minutes just to read status.

| Level | What | 2FA |
|---|---|---|
| 🟢 bot read-only | status, services, panel, traffic, subscriptions, history, audit | none |
| 📱 Mini App | dashboard login | TOTP code for each new session |
| 🟡 actions | service/xray restart, `apt update`, allowlisted command | code once, 15-minute unlock |
| 🔴 destructive | `apt full/phased`, release upgrade, reboot | **fresh** code (max 2 min old) |

Flow:

1. Open the Mini App → “Confirm login” → enter 6 digits.
2. In the bot tap “Restart x-ui” → “🔐 Enter the 6-digit code”.
3. The bot deletes the code message, verifies it and says
   “🔓 Actions unlocked until 14:35”.
4. The usual confirmation appears. For the next 15 minutes dangerous actions do
   not ask for a code (destructive actions always need a fresh one).
5. “🔐 Security” in the menu shows status, unlock and lock.

Enable: the installer asks (default yes), or use `--2fa`. The secret is shown
when generated; add it to Google Authenticator/Aegis/1Password. Show it later:
`sudo stacksentry-menu totp` (root only). Protection: one-time code within the
window, 3 attempts, 5-minute lockout, every success/failure is audited. The
secret lives in `.env` with `640 root:<service group>` and is scrubbed from logs.
The terminal uses a separate TOTP secret and asks for a code for every session.

## Secrets and storage

- Bot and panel tokens live in `.env` with `640 root:<service group>`; the
  service can read but not rewrite the file.
- The terminal (if enabled) keeps its secrets separately:
  `/etc/stacksentry/terminal.env`, `600 root`; systemd passes them via
  `EnvironmentFile`, so the service user never reads the file.
- Logs are scrubbed: the bot token in URLs, panel/terminal tokens, TOTP and the
  access token are removed; `initData` is never logged; Telegram IDs are masked.
- `state.db` is `0600`; it contains Telegram IDs, audit and subscriptions.
- Application-level encryption with a key on the same host adds little: a host
  compromise reveals the key. Use LUKS/VPS disk encryption and encrypted backups.
  For the terminal, add a second factor (TOTP is already there) and access via
  VPN/SSH tunnel.

## Security

- The bot runs as the unprivileged `stacksentry` user; code and venv belong to
  `root`, and `.env` is read-only for the service.
- All privileged operations go through `sudo stacksentry-ctl` and a narrow sudoers list of
  **exact commands** (no wildcards in arguments, as required by both classic sudo
  and `sudo-rs`). There is no arbitrary shell.
- `apt` runs in dedicated `stacksentry-apt-*.service` units that do not inherit the
  bot's sandbox; a fallback exists when a transient unit is denied.
- Risky actions require confirmation; the token is bound to the requesting
  administrator, is single-use, and everything is written to `audit_log`.
- The terminal requires TOTP for every session: `initData` alone is not enough
  if the Telegram account is compromised. The root session is additionally
  enabled only with `--root-terminal`; the terminal SSH key stays on the server
  and is never exposed.
- The panel client does not follow redirects, panel values are escaped, and the
  bot token never reaches the logs even at `DEBUG`.
- Repeats are limited by `ACTION_COOLDOWN`; Telegram flood control is retried with
  a pause, and long messages are split by lines.
- Commands are accepted only in private chat; outsider attempts are audited
  (`denied`).
- The installer can verify the archive checksum (`--sha256`).
- Secrets are stripped from logs by a formatter (including exception text),
  Telegram IDs are masked (`…789`), and `stacksentry-menu show/check` never print secrets.
- For a self-signed panel certificate use `PANEL_CA_BUNDLE` instead of disabling
  TLS verification.

Service resources are limited (`MemoryMax=256M`, `TasksMax=64`), command output is
truncated (16 KB) and has a timeout.

## Self-diagnostics

```bash
sudo stacksentry-doctor              # config, database, services, panel
sudo stacksentry-doctor --no-network # skip panel requests
```

## Update and removal

```bash
# update (config and state are kept)
bash <(curl -Ls https://raw.githubusercontent.com/Hijackery/stacksentry/main/install.sh)

# same from the menu
sudo stacksentry-menu          # item 24

# check for updates without installing anything
sudo stacksentry-menu update-check
# or in the bot: /updatecheck / Help → “🔄 Check for updates”

# in the bot: when an update exists an “⬆️ Update StackSentry” button appears
# (confirmation + fresh TOTP; .env and state are kept)

# full removal: service, sudoers, wrapper, menu, code, database and user
sudo bash install.sh --uninstall

# keep the database and user
sudo bash install.sh --uninstall --keep-state

# installer's interactive menu
sudo bash install.sh --menu
```

Removal prints a single “Bot removed.” line and closes `stacksentry-menu`. With
`--keep-state` a warning remains that the database and user were preserved.

> The project publishes no releases or tags: the current version is always the
> `main` branch. GitHub may serve it from cache for a couple of minutes, so
> re-run the installer.

## Troubleshooting

```bash
systemctl status stacksentry-bot
journalctl -u stacksentry-bot -n 50 --no-pager
```

| Symptom | What to do |
|---|---|
| “Invalid sudoers syntax” | the installer prints the `visudo` line, attach it to an issue |
| “System metrics unavailable: `/proc/stat`” | an old install still has `ProcSubset=pid`: `sudo sed -i '/^ProcSubset=/d' /etc/systemd/system/stacksentry-bot.service && sudo systemctl daemon-reload && sudo systemctl restart stacksentry-bot` |
| “Token rejected” | check `TELEGRAM_BOT_TOKEN` and `PANEL_API_TOKEN` via `sudo stacksentry-menu` → “Show settings” |
| `inbounds/list unavailable` | the panel token lacks the `node-sync` scope |
| Panel returns `404` for the API | check `PANEL_URL`/`PANEL_BASE_PATH` (a common mistake is `/panel` in `webBasePath`) |
| Service logs are not visible | check the `journal` sudoers rule and that the call goes through `sudo` |
| Ubuntu 26.04+ | `sudo-rs` forbids wildcards in arguments, and the installer already accounts for it |

### No Mini App button

Check the settings and the log:

```bash
grep -E '^WEBAPP_(ENABLED|PUBLIC_ORIGIN)=' /opt/stacksentry/.env
sudo stacksentry-menu webapp
systemctl status stacksentry-bot stacksentry-webapp caddy
journalctl -u stacksentry-bot -n 100 --no-pager | grep -i 'Mini App'
```

The buttons appear only with `WEBAPP_ENABLED=1` and a non-empty
`WEBAPP_PUBLIC_ORIGIN` (HTTPS), and the inline button is shown only to an
administrator from `TELEGRAM_ADMIN_IDS`. If the origin is empty, reinstall the
Mini App without a domain: `sudo bash install.sh --webapp --no-terminal`. The
installer sets up `sslip.io` + Caddy automatically. If the origin is set but
there is no button:

- bind the domain via `@BotFather` → `/setdomain`
  (for `sslip.io`, host only, without the port);
- the public URL must open over HTTPS (check Caddy/proxy in front of
  `127.0.0.1:8787`);
- restart the bot after editing `.env`:
  `sudo systemctl restart stacksentry-bot`;
- `Не удалось поставить кнопку Mini App` in the log means Telegram rejected the
  button, usually because of a wrong domain, HTTP instead of HTTPS, or an
  unreachable URL.

More in [SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md).

## Documentation

- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md): OS, init, packages, panels, protocols, restart.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, security, extension.
- [docs/RELEASING.md](docs/RELEASING.md): versions, checking and applying updates.
- [SUPPORT.md](SUPPORT.md): where to ask and what to include.
- [SECURITY.md](SECURITY.md): security policy and model.
- [CONTRIBUTING.md](CONTRIBUTING.md): setup, checks, PRs.
- [CHANGELOG.md](CHANGELOG.md): change history.

## Versions and updates

- SemVer; the current version lives in `stacksentry/__init__.py` and `/version`.
- GitHub Releases and tags are **not published**: `main` is the current version.
- Check for updates: the “🔄 Check for updates” button in Help, the Mini App
  Help tab, `sudo stacksentry-menu update-check`, or `/updatecheck`.
- When an update exists, the bot shows “⬆️ Update StackSentry”. It requires
  confirmation and a fresh TOTP (when 2FA is enabled), keeps `.env` and state,
  then restarts the bot.
- Manual update: `sudo stacksentry-menu update` or re-run the installer. Details
  and rollback: [docs/RELEASING.md](docs/RELEASING.md).

## Development

```bash
pip install -r requirements-dev.txt
ruff format --check .
ruff check .
mypy .
pytest -q
```

Tests need no panel, Telegram or systemd: external calls are mocked. CI runs
Python 3.10 and 3.13, `shellcheck`, `bash -n` and `visudo -cf`.

## Project

- [SECURITY.md](SECURITY.md): how to report a vulnerability and what is protected.
- [CONTRIBUTING.md](CONTRIBUTING.md): environment, required checks, style.
- [SUPPORT.md](SUPPORT.md): where to ask and what to include.
- [docs/](docs/README.md): compatibility, architecture, updates.
- [CHANGELOG.md](CHANGELOG.md): change history.
- [`.env.example`](.env.example): every setting with comments.
- [Русский README](README.md).

## License

MIT, see [LICENSE](LICENSE).
