"""Mini App: cookie, initData, read-only API, CSP, конфиг."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from stacksentry import __version__
from stacksentry.models import MonitorSnapshot, PanelHealth, SystemMetrics
from stacksentry.settings import ConfigError, Settings, load_settings
from stacksentry.storage import Storage
from stacksentry.totp import totp_code
from stacksentry.update_spool import (
    read_json,
    request_path,
    status_path,
    write_status,
)
from stacksentry.updates import UpdateInfo
from stacksentry.webapp import (
    WebAppConfig,
    create_app,
    issue_cookie,
    verify_cookie,
)

ADMIN_ID = 42
SECRET = "w" * 40
TOTP_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _config(
    settings: Settings, *, origin: str = "", terminal_origin: str = ""
) -> WebAppConfig:
    return WebAppConfig(
        settings=settings,
        secret=SECRET,
        host="127.0.0.1",
        port=8787,
        public_origin=origin,
        session_ttl=3600,
        terminal_origin=terminal_origin,
    )


def _init_data(
    token: str, user_id: int = ADMIN_ID, *, nonce: str = "webapp-test"
) -> str:
    data = {
        "auth_date": str(int(time.time())),
        "query_id": nonce,
        "user": json.dumps(
            {"id": user_id, "first_name": "Admin"}, separators=(",", ":")
        ),
    }
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**data, "hash": digest})


class FakeMonitor:
    async def snapshot(self) -> MonitorSnapshot:
        return MonitorSnapshot(
            system=SystemMetrics(
                cpu_percent=12.0,
                mem_percent=40.0,
                mem_total_bytes=1024**3,
                swap_percent=0.0,
                disk_percent=30.0,
                disk_free_bytes=10**9,
                load1=0.1,
                uptime_seconds=100.0,
                tcp_connections=1,
                net_sent_bytes=0,
                net_recv_bytes=0,
            ),
            services=[],
            panel=PanelHealth(
                reachable=False,
                status_code=None,
                latency_ms=None,
                error=None,
                disabled=True,
            ),
            inbounds=[],
            subscriptions=[],
        )


def test_cookie_roundtrip_and_tamper() -> None:
    cookie = issue_cookie(SECRET, ADMIN_ID, 3600)
    assert verify_cookie(SECRET, cookie) == ADMIN_ID
    assert verify_cookie(SECRET, cookie + "0") is None
    assert verify_cookie(SECRET, issue_cookie(SECRET, ADMIN_ID, -1)) is None
    assert verify_cookie("x" * 40, cookie) is None


def test_session_and_read_only_api(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    settings = make_settings(
        webapp_enabled=True, webapp_secret=SECRET, webapp_public_origin=""
    )
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)

    assert client.get("/api/status").status_code == 401
    assert client.post("/api/session", json={"initData": "bad"}).status_code == 401

    response = client.post(
        "/api/session", json={"initData": _init_data(settings.telegram_token)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["id"] == ADMIN_ID

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["panel"]["disabled"] is True
    assert "Content-Security-Policy" in status.headers
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/audit").status_code == 200


def test_terminal_tab_origin_and_csp(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    settings = make_settings(webapp_enabled=True, webapp_secret=SECRET)
    app = create_app(
        _config(settings, terminal_origin="https://term.example.com"),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    client.post("/api/session", json={"initData": _init_data(settings.telegram_token)})
    meta = client.get("/api/meta")
    assert meta.status_code == 200
    assert meta.json()["terminal_origin"] == "https://term.example.com"
    assert meta.json()["version"]
    policy = meta.headers["Content-Security-Policy"]
    assert "frame-src 'self' https://term.example.com" in policy


def test_logs_endpoint_only_for_watched_services(
    make_settings: Callable[..., Settings],
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(webapp_enabled=True, webapp_secret=SECRET)
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    client.post("/api/session", json={"initData": _init_data(settings.telegram_token)})

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "journalctl"
        return subprocess.CompletedProcess(argv, 0, "line one\nline two\n", "")

    monkeypatch.setattr("stacksentry.webapp.subprocess.run", fake_run)
    response = client.get("/api/logs?service=x-ui&lines=20")
    assert response.status_code == 200
    assert "line two" in response.json()["text"]
    assert client.get("/api/logs?service=ssh").status_code == 404


def test_update_request_creates_spool_and_status(
    make_settings: Callable[..., Settings],
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = Path("/tmp") / "stacksentry-webapp-update-test"
    settings = make_settings(
        webapp_enabled=True,
        webapp_secret=SECRET,
        update_spool_dir=spool,
    )
    info = UpdateInfo(
        current_version="1.0.0",
        current_sha="a" * 40,
        remote_sha="b" * 40,
        remote_version="1.1.0",
        remote_date="29.10.2026 10:00 UTC",
        remote_message="Обновление",
        update_available=True,
        repo="Hijackery/stacksentry",
    )
    monkeypatch.setattr("stacksentry.webapp.check_for_update", lambda **_kw: info)
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    client.post("/api/session", json={"initData": _init_data(settings.telegram_token)})

    created = client.post("/api/update/request")
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]
    assert request_path(spool, request_id).is_file()
    assert read_json(request_path(spool, request_id))["user_id"] == ADMIN_ID  # type: ignore[index]

    queued = client.get(f"/api/update/status?request={request_id}")
    assert queued.status_code == 200
    assert queued.json()["state"] == "queued"

    write_status(
        spool,
        request_id,
        state="awaiting_confirmation",
        progress=20,
        stage="Ожидает подтверждения",
    )
    confirmed = client.get(f"/api/update/status?request={request_id}")
    assert confirmed.json()["state"] == "awaiting_confirmation"
    assert confirmed.json()["progress"] == 20
    assert status_path(spool, request_id).is_file()


def test_metrics_and_health_endpoints(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    settings = make_settings(webapp_enabled=True, webapp_secret=SECRET)
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200
    assert "no-store" in index.headers["Cache-Control"]
    assert f"/app.js?v={__version__}" in index.text
    assert f"v{__version__}" in index.text or "__VERSION__" not in index.text
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "stacksentry_up 1" in metrics.text
    assert "stacksentry_build_info" in metrics.text

    guarded = make_settings(
        webapp_enabled=True,
        webapp_secret=SECRET,
        webapp_metrics_token="m" * 20,
    )
    guarded_app = create_app(
        _config(guarded),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    guarded_client = TestClient(guarded_app)
    assert guarded_client.get("/metrics").status_code == 401
    assert (
        guarded_client.get(
            "/metrics", headers={"Authorization": "Bearer " + "m" * 20}
        ).status_code
        == 200
    )


def test_update_endpoint_is_cached_and_health_is_public(
    make_settings: Callable[..., Settings],
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(webapp_enabled=True, webapp_secret=SECRET)
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    calls: list[int] = []

    def fake_update(**_kwargs: object) -> UpdateInfo:
        calls.append(1)
        return UpdateInfo(
            current_version="1.0.0",
            current_sha="a" * 40,
            remote_sha="b" * 40,
            remote_version="1.1.0",
            remote_date="29.10.2026 10:00 UTC",
            remote_message="Обновление",
            update_available=True,
            repo="Hijackery/stacksentry",
        )

    monkeypatch.setattr("stacksentry.webapp.check_for_update", fake_update)
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/update").status_code == 401
    client.post("/api/session", json={"initData": _init_data(settings.telegram_token)})

    first = client.get("/api/update")
    second = client.get("/api/update")

    assert first.status_code == 200
    assert first.json()["update_available"] is True
    assert first.json()["current_sha"] == "aaaaaaa"
    assert second.json() == first.json()
    assert calls == [1]


def test_2fa_gate_for_webapp(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    settings = make_settings(
        webapp_enabled=True,
        webapp_secret=SECRET,
        require_2fa=True,
        totp_secret=TOTP_SECRET,
    )
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    init_data = _init_data(settings.telegram_token)

    no_code = client.post("/api/session", json={"initData": init_data})
    assert no_code.status_code == 401
    assert no_code.json()["detail"]["code"] == "totp_required"

    wrong = client.post("/api/session", json={"initData": init_data, "totp": "000000"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "totp_invalid"

    code = totp_code(TOTP_SECRET)
    accepted = client.post("/api/session", json={"initData": init_data, "totp": code})
    assert accepted.status_code == 200, accepted.text
    assert client.get("/api/status").status_code == 200

    replay = TestClient(app).post(
        "/api/session", json={"initData": init_data, "totp": code}
    )
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "totp_replay"


def test_non_admin_is_rejected(
    make_settings: Callable[..., Settings], storage: Storage
) -> None:
    settings = make_settings(webapp_enabled=True, webapp_secret=SECRET)
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    response = client.post(
        "/api/session", json={"initData": _init_data(settings.telegram_token, 999)}
    )
    assert response.status_code == 403


def test_config_requires_secret(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    base = (
        "TELEGRAM_BOT_TOKEN=123456:ABC\n"
        "TELEGRAM_ADMIN_IDS=42\n"
        "PANEL_URL=https://panel.example.com\n"
        "PANEL_API_TOKEN=t\n"
        "SERVICES=x-ui\n"
        "WEBAPP_ENABLED=1\n"
    )
    env.write_text(base + "WEBAPP_SECRET=short\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="WEBAPP_SECRET"):
        load_settings(env)
    env.write_text(base + f"WEBAPP_SECRET={SECRET}\n", encoding="utf-8")
    config = WebAppConfig.load(env)
    assert config.secret == SECRET


def test_installer_webapp_flag() -> None:
    from stacksentry.settings import BASE_DIR

    text = (BASE_DIR / "install.sh").read_text(encoding="utf-8")
    assert "--webapp" in text
    assert "stacksentry-webapp.service" in text
    assert "create_app_from_env" in text
    assert "WEBAPP_TERMINAL_ORIGIN" in text
    assert "terminal-ssh" in text
    assert 'TERM_SSH_DIR="$TERM_HOME/terminal-ssh"' in text
    assert 'chmod 600 "$TERM_SSH_KEY"' in text


def test_mini_app_has_help_tab() -> None:
    from stacksentry.settings import BASE_DIR

    js = (BASE_DIR / "stacksentry" / "webapp_static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert '["help", "Помощь"]' in js
    assert '["updates", "Обновления"]' in js
    assert "function renderUpdatesPage()" in js
    assert "function renderHelp()" in js
    assert 'state.active === "help"' in js
    assert "Если 2FA выключена, TOTP не запрашивается нигде" in js
    assert "update-banner" in js
    assert "Новая версия StackSentry" in js
    assert "loadUpdate" in js
    assert 'input.value = ""' in js
    assert "stacksentry.pins" in js
    assert "Настроить дашборд" in js
    assert "renderServiceDetail" in js
    assert "renderMetricDetail" in js
    assert "global-search" in js
    assert "data-request-update" in js
    assert "/api/update/status" in js
    assert "update-progress" in js
    assert "data-open-update" in js


def test_cookie_flags(make_settings: Callable[..., Settings], storage: Storage) -> None:
    settings = make_settings(webapp_enabled=True, webapp_secret=SECRET)
    app = create_app(
        _config(settings),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    client = TestClient(app)
    response = client.post(
        "/api/session", json={"initData": _init_data(settings.telegram_token)}
    )
    cookie = response.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie

    secure_app = create_app(
        _config(settings, origin="https://app.example.com"),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        storage=storage,
    )
    secure_response = TestClient(secure_app).post(
        "/api/session",
        json={"initData": _init_data(settings.telegram_token, nonce="secure")},
    )
    secure_cookie = secure_response.headers.get("set-cookie", "").lower()
    assert "secure" in secure_cookie
    assert "samesite=none" in secure_cookie


def test_webapp_uses_own_state_path_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "web.env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=123456789:AAtest",
                "TELEGRAM_ADMIN_IDS=42",
                "WEBAPP_ENABLED=1",
                f"WEBAPP_SECRET={SECRET}",
                "STATE_PATH=/var/lib/stacksentry/state.db",
            ]
        ),
        encoding="utf-8",
    )
    own_state = tmp_path / "webapp" / "state.db"
    monkeypatch.setenv("WEBAPP_STATE_PATH", str(own_state))

    config = WebAppConfig.load(env_file)

    assert config.settings.state_path == own_state
