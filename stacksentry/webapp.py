"""Read-only Telegram Mini App: auth initData + JSON API + статический дашборд.

Сервис работает от отдельного непривилегированного пользователя и только
читает состояние. Действий здесь нет — они остаются в боте/терминале.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .monitor import Monitor
from .settings import ConfigError, Settings, load_settings
from .storage import Storage
from .terminal.auth import RateLimiter, validate_init_data
from .totp import ReplayGuard, normalize_totp, verify_totp
from .update_spool import (
    read_json,
    read_status,
    request_path,
    valid_id,
    write_json,
    write_status,
)
from .updates import check_for_update

COOKIE_NAME = "stacksentry_session"
STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"


@dataclass(frozen=True, slots=True)
class WebAppConfig:
    settings: Settings
    secret: str
    host: str
    port: int
    public_origin: str
    session_ttl: int
    terminal_origin: str = ""

    @classmethod
    def load(cls, env_file: Path | None = None) -> WebAppConfig:
        settings = load_settings(env_file)
        # Отдельная база Mini App. Не используем STATE_PATH: он уже задан в
        # .env для бота, а systemd EnvironmentFile перебивает Environment=.
        webapp_state = os.getenv("WEBAPP_STATE_PATH", "").strip()
        if webapp_state:
            settings = replace(settings, state_path=Path(webapp_state))
        if len(settings.webapp_secret) < 32:
            raise ConfigError("WEBAPP_SECRET: нужен случайный секрет ≥ 32 символов")
        if (
            settings.webapp_public_origin
            and not settings.webapp_public_origin.startswith("https://")
        ):
            raise ConfigError("WEBAPP_PUBLIC_ORIGIN должен начинаться с https://")
        return cls(
            settings=settings,
            secret=settings.webapp_secret,
            host=settings.webapp_host,
            port=settings.webapp_port,
            public_origin=settings.webapp_public_origin,
            session_ttl=max(300, settings.webapp_session_ttl),
            terminal_origin=settings.webapp_terminal_origin,
        )


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def issue_cookie(secret: str, user_id: int, ttl: int) -> str:
    now = time.time()
    ttl = min(ttl, 86400)
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "uid": user_id,
                    "iat": now,
                    "exp": now + ttl,
                    "jti": secrets.token_urlsafe(12),
                },
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{body}.{_sign(secret, body)}"


def verify_cookie(secret: str, cookie: str, *, now: float | None = None) -> int | None:
    body, sep, signature = cookie.partition(".")
    if not sep:
        return None
    try:
        matched = hmac.compare_digest(_sign(secret, body).encode(), signature.encode())
    except (TypeError, UnicodeError):
        matched = False
    if not matched:
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(body + "=" * ((4 - len(body) % 4) % 4))
        )
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    issued = payload.get("iat")
    expires = payload.get("exp")
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or not isinstance(issued, (int, float))
        or isinstance(issued, bool)
    ):
        return None
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return None
    moment = time.time() if now is None else now
    # Ограничиваем срок жизни на сервере, даже если cookie подписали иначе.
    if issued > moment + 60 or expires <= moment or expires - issued > 86400:
        return None
    return uid


def _snapshot_json(snapshot: Any, age: float) -> dict[str, Any]:
    return {
        "system": asdict(snapshot.system) if snapshot.system else None,
        "services": [asdict(item) for item in snapshot.services],
        "panel": asdict(snapshot.panel),
        "inbounds": [asdict(item) for item in snapshot.inbounds],
        "subscriptions": [asdict(item) for item in snapshot.subscriptions],
        "checks": [asdict(item) for item in snapshot.checks],
        "warnings": list(snapshot.warnings),
        "unavailable": sorted(snapshot.unavailable),
        "total_used_bytes": snapshot.total_used_bytes,
        "active_clients": snapshot.active_clients,
        "age": round(time.monotonic() - age, 1),
    }


MAX_AUTH_BODY = 64 * 1024


class SessionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    init_data: str = Field(default="", alias="initData", max_length=8192)
    totp: str = Field(default="", max_length=32)


def create_app(
    config: WebAppConfig,
    *,
    monitor: Monitor | None = None,
    storage: Storage | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    monitor = monitor or Monitor(config.settings)
    storage = storage or Storage(config.settings.state_path)
    snapshot_cache: dict[str, Any] = {"value": None, "at": 0.0}
    snapshot_lock = asyncio.Lock()
    update_cache: dict[str, Any] = {"value": None, "at": 0.0}
    update_lock = asyncio.Lock()
    totp_replay = ReplayGuard(ttl=120.0, claim=storage.claim_replay)
    init_replay = ReplayGuard(ttl=360.0, claim=storage.claim_replay)
    totp_limiter = RateLimiter(limit=5, window=60.0)
    preauth_limiter = RateLimiter(limit=20, window=60.0)

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Any:
        if request.method == "POST" and request.url.path == "/api/session":
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > MAX_AUTH_BODY:
                return JSONResponse(
                    {"detail": "слишком большой запрос"}, status_code=413
                )
        # Read-only API не должен принимать cross-site запросы даже без CORS.
        if (
            config.public_origin
            and request.url.path.startswith("/api/")
            and request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse(
                {"detail": "cross-site запрос запрещён"}, status_code=403
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        frame_src = "'self'"
        if config.terminal_origin:
            frame_src = f"{frame_src} {config.terminal_origin}"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' "
            "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            f"frame-src {frame_src}; base-uri 'none'; frame-ancestors "
            "https://web.telegram.org https://*.telegram.org"
        )
        if config.public_origin:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def current_user(request: Request) -> int:
        cookie = request.cookies.get(COOKIE_NAME, "")
        uid = verify_cookie(config.secret, cookie) if cookie else None
        if uid is None or uid not in config.settings.admin_ids:
            raise HTTPException(status_code=401, detail="нужен вход через Telegram")
        return uid

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/api/session")
    async def session(
        request: Request, body: SessionRequest, response: Response
    ) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        if not preauth_limiter.allow(client):
            raise HTTPException(status_code=429, detail="слишком много попыток")
        origin = request.headers.get("origin", "")
        if config.public_origin and origin and origin != config.public_origin:
            raise HTTPException(status_code=403, detail="чужой Origin")
        try:
            user = validate_init_data(body.init_data, config.settings.telegram_token)
        except Exception as exc:  # noqa: BLE001 — безопасный текст
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        user_id = int(user["id"])
        if user_id not in config.settings.admin_ids:
            raise HTTPException(status_code=403, detail="не администратор")
        if config.settings.require_2fa:
            code = normalize_totp(body.totp)
            if not code:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "totp_required",
                        "message": "Введите код 2FA из приложения-аутентификатора",
                    },
                )
            client = request.client.host if request.client else "unknown"
            if not totp_limiter.allow(f"{client}:{user_id}"):
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "totp_locked",
                        "message": "Слишком много попыток. Подождите минуту.",
                    },
                )
            if len(code) != 6 or not verify_totp(config.settings.totp_secret, code):
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "totp_invalid",
                        "message": "Неверный код 2FA",
                    },
                )
            if not totp_replay.allow(f"{user_id}:{code}"):
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "totp_replay",
                        "message": "Этот код уже использован. Дождитесь следующего.",
                    },
                )
        init_key = "init:" + hashlib.sha256(body.init_data.encode()).hexdigest()
        if not init_replay.allow(init_key):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "init_replay",
                    "message": "Mini App уже авторизован — откройте его заново.",
                },
            )
        cookie = issue_cookie(config.secret, user_id, config.session_ttl)
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            max_age=config.session_ttl,
            httponly=True,
            # Telegram Web — iframe: строгий SameSite не отправит cookie.
            secure=bool(config.public_origin),
            samesite="none" if config.public_origin else "lax",
            path="/",
        )
        return {
            "user": {
                "id": user_id,
                "name": str(user.get("first_name") or user.get("username") or ""),
            },
            "panel_enabled": config.settings.panel_enabled,
            "services": list(config.settings.services),
        }

    @app.get("/api/meta")
    async def meta(user_id: int = Depends(current_user)) -> dict[str, Any]:
        return {
            "version": __version__,
            "panel_enabled": config.settings.panel_enabled,
            "services": list(config.settings.services),
            "require_2fa": config.settings.require_2fa,
            "terminal_origin": config.terminal_origin,
        }

    async def update_payload(*, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        cached = update_cache["value"]
        if not force and cached is not None and now - update_cache["at"] <= 900:
            return cast(dict[str, Any], cached)
        async with update_lock:
            now = time.monotonic()
            cached = update_cache["value"]
            if not force and cached is not None and now - update_cache["at"] <= 900:
                return cast(dict[str, Any], cached)
            info = await asyncio.to_thread(check_for_update)
            payload = {
                "current_version": info.current_version,
                "current_sha": info.current_short,
                "remote_sha": info.remote_short,
                "remote_version": info.remote_version,
                "remote_date": info.remote_date,
                "remote_message": info.remote_message,
                "update_available": info.update_available,
                "repo": info.repo,
                "error": info.error,
                "checked_at": time.time(),
            }
            update_cache["value"] = payload
            update_cache["at"] = time.monotonic()
            return payload

    @app.get("/api/update")
    async def update(
        force: bool = False, user_id: int = Depends(current_user)
    ) -> dict[str, Any]:
        """Проверка обновлений StackSentry без права что-либо устанавливать."""
        return await update_payload(force=force)

    @app.post("/api/update/request")
    async def update_request(user_id: int = Depends(current_user)) -> dict[str, Any]:
        """Создаёт запрос: бот попросит админа подтвердить обновление."""
        info = await update_payload(force=True)
        if info.get("update_available") is not True:
            raise HTTPException(status_code=409, detail="обновление не найдено")
        request_id = secrets.token_urlsafe(12)
        payload = {
            "id": request_id,
            "user_id": user_id,
            "created_at": time.time(),
            "current_version": info.get("current_version", ""),
            "target_version": info.get("remote_version", ""),
            "target_sha": info.get("remote_sha", ""),
        }
        spool = config.settings.update_spool_dir
        await asyncio.to_thread(write_json, request_path(spool, request_id), payload)
        await asyncio.to_thread(
            write_status,
            spool,
            request_id,
            state="queued",
            progress=5,
            stage="Запрос создан",
            detail="Ожидаю подтверждения в Telegram.",
            target_sha=str(payload["target_sha"]),
            target_version=str(payload["target_version"]),
        )
        return {"id": request_id, "state": "queued", "progress": 5}

    @app.get("/api/update/status")
    async def update_status(
        request: str, user_id: int = Depends(current_user)
    ) -> dict[str, Any]:
        if not valid_id(request):
            raise HTTPException(status_code=400, detail="некорректный запрос")
        spool = config.settings.update_spool_dir
        status = await asyncio.to_thread(read_status, spool, request)
        if status is not None:
            return status
        pending = await asyncio.to_thread(read_json, request_path(spool, request))
        if pending is not None:
            return {
                "id": request,
                "state": "queued",
                "progress": 5,
                "stage": "Запрос создан",
                "detail": "Ожидаю подтверждения в Telegram.",
            }
        return {
            "id": request,
            "state": "unknown",
            "progress": 0,
            "stage": "Статус не найден",
            "detail": "Запрос устарел или уже завершён.",
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> PlainTextResponse:
        token = config.settings.webapp_metrics_token
        if token:
            auth = request.headers.get("authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                raise HTTPException(status_code=401, detail="нужен токен метрик")
        samples = await asyncio.to_thread(storage.count_samples)
        pending = await asyncio.to_thread(storage.count_pending_alerts)
        body = (
            "\n".join(
                (
                    "# HELP stacksentry_up StackSentry web process is alive",
                    "# TYPE stacksentry_up gauge",
                    "stacksentry_up 1",
                    "# HELP stacksentry_build_info Build information",
                    "# TYPE stacksentry_build_info gauge",
                    f'stacksentry_build_info{{version="{__version__}"}} 1',
                    "# HELP stacksentry_samples_total Stored metric samples",
                    "# TYPE stacksentry_samples_total gauge",
                    f"stacksentry_samples_total {samples}",
                    "# HELP stacksentry_alert_outbox_pending Pending alerts",
                    "# TYPE stacksentry_alert_outbox_pending gauge",
                    f"stacksentry_alert_outbox_pending {pending}",
                )
            )
            + "\n"
        )
        return PlainTextResponse(
            body, media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    @app.get("/api/status")
    async def status(user_id: int = Depends(current_user)) -> dict[str, Any]:
        now = time.monotonic()
        if snapshot_cache["value"] is not None and now - snapshot_cache["at"] <= 15:
            return _snapshot_json(snapshot_cache["value"], snapshot_cache["at"])
        # Single-flight: первый запрос собирает снимок, остальные ждут его,
        # а не запускают параллельно ещё несколько опросов панели/systemctl.
        async with snapshot_lock:
            now = time.monotonic()
            if snapshot_cache["value"] is None or now - snapshot_cache["at"] > 15:
                snapshot_cache["value"] = await monitor.snapshot()
                snapshot_cache["at"] = time.monotonic()
        return _snapshot_json(snapshot_cache["value"], snapshot_cache["at"])

    @app.get("/api/logs")
    async def logs(
        service: str, lines: int = 80, user_id: int = Depends(current_user)
    ) -> dict[str, Any]:
        if service not in config.settings.services:
            raise HTTPException(status_code=404, detail="служба не наблюдается")
        limit = max(10, min(lines, 200))

        def read() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "journalctl",
                    "-u",
                    service,
                    "-n",
                    str(limit),
                    "--no-pager",
                    "--output=short-iso",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        try:
            proc = await asyncio.to_thread(read)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=(proc.stderr or "journalctl недоступен").strip()[:300],
            )
        return {"service": service, "text": proc.stdout[-12000:], "lines": limit}

    @app.get("/api/history")
    async def history(hours: int = 24, user_id: int = Depends(current_user)) -> Any:
        hours = max(1, min(hours, 720))
        rows = await asyncio.to_thread(
            storage.get_series, time.time() - hours * 3600, hours
        )
        return [dict(row) for row in rows]

    @app.get("/api/audit")
    async def audit(limit: int = 30, user_id: int = Depends(current_user)) -> Any:
        limit = max(1, min(limit, 200))
        rows = await asyncio.to_thread(storage.recent_audit, limit)
        return [
            {
                "ts": row["ts"],
                "action": row["action"],
                "ok": bool(row["ok"]),
                "result": row["result"],
            }
            for row in rows
        ]

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def create_app_from_env() -> FastAPI:
    return create_app(WebAppConfig.load())
