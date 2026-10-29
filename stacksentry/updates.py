"""Проверка обновлений StackSentry без GitHub Releases.

Релизы и теги проект не публикует: актуальная версия — ветка ``main``.
Проверка сравнивает установленный commit с последним commit ``main``; если
метаданных установки нет, остаётся сравнение версий из ``__init__.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .settings import BASE_DIR

DEFAULT_REPO = "Hijackery/stacksentry"
INSTALL_FILE = BASE_DIR / ".stacksentry-install"
_API = "https://api.github.com/repos"
_RAW = "https://raw.githubusercontent.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class InstallInfo:
    version: str
    ref: str
    sha: str
    installed_at: str
    repo: str


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    current_sha: str
    remote_sha: str
    remote_version: str
    remote_date: str
    remote_message: str
    update_available: bool | None
    repo: str
    error: str | None = None

    @property
    def current_short(self) -> str:
        return self.current_sha[:7] if self.current_sha else "неизвестен"

    @property
    def remote_short(self) -> str:
        return self.remote_sha[:7] if self.remote_sha else "неизвестен"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip()] = value.strip()
    return values


def _git_sha(base: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def read_install_info(path: Path | None = None) -> InstallInfo:
    """Метаданные установки; для dev-checkout берём commit из git."""
    data = _parse_env_file(path or INSTALL_FILE)
    # Версию берём из работающего кода: метаданные могли остаться от старой
    # установки, и тогда UI показывал «1.0.0» при уже установленном 1.1.x.
    version = __version__
    ref = data.get("REF", "") or "main"
    sha = data.get("SHA", "") or _git_sha(BASE_DIR)
    repo = data.get("REPO", "") or DEFAULT_REPO
    if not _REPO_RE.fullmatch(repo):
        repo = DEFAULT_REPO
    return InstallInfo(
        version=version,
        ref=ref,
        sha=sha,
        installed_at=data.get("INSTALLED_AT", ""),
        repo=repo,
    )


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(10.0),
        follow_redirects=False,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"StackSentry/{__version__}",
        },
    )


def _remote_commit(repo: str, ref: str) -> dict[str, Any]:
    with _client() as client:
        response = client.get(f"{_API}/{repo}/commits/{ref}")
    if response.status_code == 403:
        raise RuntimeError("GitHub API ограничил запросы (HTTP 403); повторите позже")
    if response.status_code != 200:
        raise RuntimeError(f"GitHub API ответил HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub API вернул неожиданный ответ")
    return payload


def _remote_version(repo: str, ref: str) -> str:
    url = f"{_RAW}/{repo}/{ref}/stacksentry/__init__.py"
    try:
        with _client() as client:
            response = client.get(url)
        if response.status_code != 200:
            return ""
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', response.text, re.MULTILINE)
        return match.group(1) if match else ""
    except httpx.HTTPError:
        return ""


def _parse_date(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%d.%m.%Y %H:%M UTC")


def check_for_update(
    *, install_path: Path | None = None, repo: str | None = None
) -> UpdateInfo:
    """Сравнить установленный commit/версию с последним commit ``main``."""
    info = read_install_info(install_path)
    target_repo = repo if repo and _REPO_RE.fullmatch(repo) else info.repo
    current_version = info.version or __version__
    current_sha = info.sha
    try:
        commit = _remote_commit(target_repo, "main")
        remote_sha = str(commit.get("sha", ""))
        commit_info = commit.get("commit")
        if not isinstance(commit_info, dict):
            commit_info = {}
        message = str(commit_info.get("message", "")).splitlines()
        remote_message = message[0] if message else ""
        author = commit_info.get("author")
        date = author.get("date") if isinstance(author, dict) else ""
        remote_date = _parse_date(date)
        remote_version = _remote_version(target_repo, "main")
    except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return UpdateInfo(
            current_version=current_version,
            current_sha=current_sha,
            remote_sha="",
            remote_version="",
            remote_date="",
            remote_message="",
            update_available=None,
            repo=target_repo,
            error=str(exc) or exc.__class__.__name__,
        )

    if current_sha and remote_sha:
        available: bool | None = current_sha != remote_sha
    elif remote_version and current_version:
        available = remote_version != current_version
    else:
        available = None
    return UpdateInfo(
        current_version=current_version,
        current_sha=current_sha,
        remote_sha=remote_sha,
        remote_version=remote_version,
        remote_date=remote_date,
        remote_message=remote_message,
        update_available=available,
        repo=target_repo,
    )


def format_update(info: UpdateInfo) -> str:
    """Короткий человеческий отчёт для бота и терминала."""
    lines = [
        f"Версия: {info.current_version}",
        f"Установленный commit: {info.current_short}",
    ]
    if info.error:
        lines.append(f"Проверка не удалась: {info.error}")
        return "\n".join(lines)
    lines.extend(
        [
            f"Последний commit main: {info.remote_short}"
            + (f" ({info.remote_date})" if info.remote_date else ""),
        ]
    )
    if info.remote_version:
        lines.append(f"Версия в main: {info.remote_version}")
    if info.remote_message:
        lines.append(f"Изменение: {info.remote_message}")
    if info.update_available is True:
        lines.append("Доступно обновление.")
    elif info.update_available is False:
        lines.append("Обновлений нет.")
    else:
        lines.append(
            "Не удалось сравнить commit'ы: обновитесь установщиком, "
            "чтобы записать метаданные."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    repo = ""
    for index, arg in enumerate(args):
        if arg == "--repo" and index + 1 < len(args):
            repo = args[index + 1]
    info = check_for_update(repo=repo)
    print(format_update(info))
    return 0 if info.error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
