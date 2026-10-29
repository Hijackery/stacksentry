"""Общая очередь запросов на обновление между Mini App и ботом."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

REQUEST_PREFIX = "request-"
STATUS_PREFIX = "status-"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MAX_JSON = 64 * 1024


def valid_id(value: str) -> bool:
    return bool(REQUEST_ID_RE.fullmatch(value))


def request_path(spool: Path, request_id: str) -> Path:
    if not valid_id(request_id):
        raise ValueError("некорректный id запроса")
    return spool / f"{REQUEST_PREFIX}{request_id}.json"


def status_path(spool: Path, request_id: str) -> Path:
    if not valid_id(request_id):
        raise ValueError("некорректный id запроса")
    return spool / f"{STATUS_PREFIX}{request_id}.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".tmp-{secrets.token_hex(8)}"
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temp, 0o660)
        os.replace(temp, path)
    finally:
        with suppress(OSError):
            temp.unlink()


def read_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_JSON:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_requests(
    spool: Path, *, max_age: float = 600.0, now: float | None = None
) -> list[tuple[Path, dict[str, Any]]]:
    moment = time.time() if now is None else now
    try:
        paths = list(spool.glob(f"{REQUEST_PREFIX}*.json"))
    except OSError:
        return []
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if moment - path.stat().st_mtime > max_age:
                continue
        except OSError:
            continue
        payload = read_json(path)
        if payload is not None:
            result.append((path, payload))
    return result


def write_status(
    spool: Path,
    request_id: str,
    *,
    state: str,
    progress: int,
    stage: str,
    detail: str = "",
    target_sha: str = "",
    target_version: str = "",
) -> None:
    write_json(
        status_path(spool, request_id),
        {
            "id": request_id,
            "state": state,
            "progress": max(0, min(100, progress)),
            "stage": stage,
            "detail": detail,
            "target_sha": target_sha,
            "target_version": target_version,
            "updated_at": time.time(),
        },
    )


def read_status(spool: Path, request_id: str) -> dict[str, Any] | None:
    return read_json(status_path(spool, request_id))


def prune_statuses(spool: Path, *, max_age: float = 7 * 86400) -> None:
    moment = time.time()
    try:
        paths = list(spool.glob(f"{STATUS_PREFIX}*.json"))
    except OSError:
        return
    for path in paths:
        try:
            if moment - path.stat().st_mtime > max_age:
                remove(path)
        except OSError:
            continue


def remove(path: Path) -> None:
    with suppress(OSError):
        path.unlink()
