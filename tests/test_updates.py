"""Проверка обновлений StackSentry без GitHub Releases."""

from __future__ import annotations

from pathlib import Path

import pytest

from stacksentry import __version__, updates
from stacksentry.ui import render_update, update_menu


def _install_file(tmp_path: Path, *, sha: str = "a" * 40) -> Path:
    path = tmp_path / ".stacksentry-install"
    path.write_text(
        f"VERSION={__version__}\n"
        "REF=main\n"
        f"SHA={sha}\n"
        "INSTALLED_AT=2026-10-21T00:00:00Z\n"
        "REPO=Hijackery/stacksentry\n",
        encoding="utf-8",
    )
    return path


def test_read_install_info(tmp_path: Path) -> None:
    info = updates.read_install_info(_install_file(tmp_path))
    assert info.version == __version__
    assert info.ref == "main"
    assert info.sha == "a" * 40
    assert info.repo == "Hijackery/stacksentry"


def test_installed_version_prefers_running_code(tmp_path: Path) -> None:
    path = tmp_path / ".stacksentry-install"
    path.write_text("VERSION=0.9.0\nSHA=abc\n", encoding="utf-8")
    assert updates.read_install_info(path).version == __version__


def test_bad_repo_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "bad"
    path.write_text("REPO=evil/repo/../x\nSHA=abc\n", encoding="utf-8")
    info = updates.read_install_info(path)
    assert info.repo == updates.DEFAULT_REPO


def test_check_reports_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _install_file(tmp_path)

    def fake_commit(repo: str, ref: str) -> dict[str, object]:
        assert repo == "Hijackery/stacksentry"
        assert ref == "main"
        return {
            "sha": "b" * 40,
            "commit": {
                "message": "feat: something\n\nbody",
                "author": {"date": "2026-10-21T12:00:00Z"},
            },
        }

    monkeypatch.setattr(updates, "_remote_commit", fake_commit)
    monkeypatch.setattr(updates, "_remote_version", lambda repo, ref: "1.1.0")
    info = updates.check_for_update(install_path=path)

    assert info.update_available is True
    assert info.remote_short == "bbbbbbb"
    assert "something" in info.remote_message
    assert info.error is None
    text = render_update(info)
    assert "Доступно обновление" in text
    assert "Обновить StackSentry" in text
    callbacks = {
        button.callback_data
        for row in update_menu(info).inline_keyboard
        for button in row
    }
    assert "ask:update_bot:" in callbacks


def test_check_reports_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _install_file(tmp_path)
    monkeypatch.setattr(
        updates,
        "_remote_commit",
        lambda repo, ref: {
            "sha": "a" * 40,
            "commit": {"message": "same", "author": {"date": ""}},
        },
    )
    monkeypatch.setattr(updates, "_remote_version", lambda repo, ref: "1.0.0")
    info = updates.check_for_update(install_path=path)
    assert info.update_available is False
    callbacks = {
        button.callback_data
        for row in update_menu(info).inline_keyboard
        for button in row
    }
    assert "ask:update_bot:" not in callbacks
    assert "Обновлений нет" in render_update(info)


def test_check_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _install_file(tmp_path)

    def boom(repo: str, ref: str) -> dict[str, object]:
        raise RuntimeError("GitHub API ответил HTTP 503")

    monkeypatch.setattr(updates, "_remote_commit", boom)
    info = updates.check_for_update(install_path=path)
    assert info.update_available is None
    assert info.error is not None
    text = render_update(info)
    assert "Проверка не удалась" in text
