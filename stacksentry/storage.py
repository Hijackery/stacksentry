"""Хранилище состояния: метрики, состояния алертов, журнал действий."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import SystemMetrics

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    cpu REAL NOT NULL,
    mem REAL NOT NULL,
    swap REAL NOT NULL,
    disk REAL NOT NULL,
    load1 REAL NOT NULL,
    tcp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    changed_at REAL NOT NULL,
    notified_at REAL
);

CREATE TABLE IF NOT EXISTS alert_mutes (
    key TEXT PRIMARY KEY,
    until REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    telegram_id INTEGER,
    action TEXT NOT NULL,
    params TEXT,
    ok INTEGER NOT NULL,
    result TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS pending_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_jobs(created_at);

CREATE TABLE IF NOT EXISTS elevations (
    telegram_id INTEGER PRIMARY KEY,
    until REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_keys (
    key TEXT PRIMARY KEY,
    expires REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_replay_expires ON replay_keys(expires);

CREATE TABLE IF NOT EXISTS screen_state (
    chat_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    state_key TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    resolve TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    sent_at REAL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_outbox_pending
    ON alert_outbox(sent_at, next_attempt);

CREATE TABLE IF NOT EXISTS alert_candidates (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    seen INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class StorageError(RuntimeError):
    """База повреждена или недоступна: бот не должен стартовать вслепую."""


class Storage:
    """Потокобезопасная обёртка над SQLite.

    Методы синхронные, но короткие; из async-кода вызываются через
    ``asyncio.to_thread``. Поэтому соединение создаётся с
    ``check_same_thread=False``, а обращения сериализуются блокировкой —
    это безопасно в пределах одного процесса.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            # Даём другим читателям/писателям шанс освободить базу: без этого
            # параллельный доступ падал с «database is locked».
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self.conn.executescript(SCHEMA)
            # Миграция баз, созданных до появления message_id.
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(pending_jobs)")
            }
            if "message_id" not in columns:
                with suppress(sqlite3.OperationalError):
                    self.conn.execute(
                        "ALTER TABLE pending_jobs ADD COLUMN message_id "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise StorageError(
                f"База {self.path} повреждена или недоступна: {exc}. "
                "Перенесите или удалите файл — бот создаст новую "
                "(история метрик и журнал действий будут потеряны)."
            ) from exc
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """База содержит журнал действий — закрываем её от других пользователей.

        umask процесса обычно уже 0077, но файл мог остаться от прежней
        установки с более широкими правами: приводим его к 0600 явно.
        """
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            with suppress(OSError):
                if candidate.exists():
                    candidate.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --------------------------------------------------------------- samples

    def record_sample(self, metrics: SystemMetrics) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO samples (ts, cpu, mem, swap, disk, load1, tcp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    metrics.collected_at,
                    metrics.cpu_percent,
                    metrics.mem_percent,
                    metrics.swap_percent,
                    metrics.disk_percent,
                    metrics.load1,
                    metrics.tcp_connections,
                ),
            )

    def prune_samples(self, older_than: float) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM samples WHERE ts < ?", (older_than,))

    def get_series(self, since: float, limit: int = 24) -> list[sqlite3.Row]:
        with self._lock:
            rows = self.conn.execute(
                """
                -- CAST обязателен: (ts / 3600) * 3600 на REAL даёт
                -- «почти одинаковые» границы часов из-за ошибок float,
                -- и группировка схлопывается в по одному ряду на сэмпл.
                SELECT CAST(ts AS INTEGER) / 3600 * 3600 AS bucket,
                       AVG(cpu) AS cpu, AVG(mem) AS mem, AVG(disk) AS disk,
                       MAX(load1) AS load1, COUNT(*) AS samples
                FROM samples
                WHERE ts >= ?
                GROUP BY bucket
                ORDER BY bucket DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        return list(reversed(rows))

    def count_samples(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
            return int(row["n"]) if row else 0

    def get_latest_sample(self) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(
                "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return row

    # ----------------------------------------------------------- alert state

    def get_alert_state(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(
                "SELECT * FROM alert_state WHERE key = ?", (key,)
            ).fetchone()
            return row

    def set_alert_state(
        self,
        key: str,
        state: str,
        *,
        changed_at: float | None = None,
    ) -> None:
        """Записывает новое состояние правила и сбрасывает отметку «отправлено».

        Сброс обязателен: пока сообщение не доставлено, правило должно
        повторяться, а cooldown считаться от последней успешной отправки.
        """
        now = changed_at if changed_at is not None else time.time()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO alert_state (key, state, changed_at, notified_at)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(key) DO UPDATE SET
                    state = excluded.state,
                    changed_at = excluded.changed_at,
                    notified_at = NULL
                """,
                (key, state, now),
            )

    def mark_alert_sent(self, key: str, *, notified_at: float | None = None) -> None:
        """Отмечает, что сообщение по ключу доставлено."""
        now = notified_at if notified_at is not None else time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE alert_state SET notified_at = ? WHERE key = ?",
                (now, key),
            )

    def all_alert_keys(self) -> list[str]:
        with self._lock:
            return [
                row["key"]
                for row in self.conn.execute("SELECT key FROM alert_state").fetchall()
            ]

    # ----------------------------------------------------------- 2FA elevation

    def set_elevation(
        self, telegram_id: int, ttl: int, *, now: float | None = None
    ) -> float:
        moment = time.time() if now is None else now
        until = moment + ttl
        with self._lock:
            self.conn.execute(
                "INSERT INTO elevations (telegram_id, until, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET "
                "until = excluded.until, created_at = excluded.created_at",
                (telegram_id, until, moment),
            )
        return until

    def elevation_until(self, telegram_id: int, *, now: float | None = None) -> float:
        moment = time.time() if now is None else now
        with self._lock:
            row = self.conn.execute(
                "SELECT until FROM elevations WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if row is None:
            return 0.0
        until = float(row["until"])
        if until <= moment:
            self.clear_elevation(telegram_id)
            return 0.0
        return until

    def clear_elevation(self, telegram_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM elevations WHERE telegram_id = ?", (telegram_id,)
            )

    # --------------------------------------------------------- pending jobs

    def add_pending_job(
        self,
        action: str,
        kind: str,
        chat_id: int,
        user_id: int,
        message_id: int = 0,
    ) -> int:
        """Запоминает фоновое обновление: после рестарта бот допишет отчёт."""
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO pending_jobs "
                    "(action, kind, chat_id, user_id, created_at, message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (action, kind, chat_id, user_id, time.time(), message_id),
                )
            except sqlite3.OperationalError:
                # Старая база без миграции: задача важнее привязки к сообщению.
                cur = self.conn.execute(
                    "INSERT INTO pending_jobs "
                    "(action, kind, chat_id, user_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (action, kind, chat_id, user_id, time.time()),
                )
            return int(cur.lastrowid or 0)

    def list_pending_jobs(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM pending_jobs ORDER BY created_at"
            ).fetchall()

    def remove_pending_job(self, job_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM pending_jobs WHERE id = ?", (job_id,))

    def prune_pending_jobs(self, older_than: float) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM pending_jobs WHERE created_at < ?", (older_than,)
            )

    # ------------------------------------------------------ alert candidates

    def get_alert_candidate(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(
                "SELECT * FROM alert_candidates WHERE key = ?", (key,)
            ).fetchone()
            return row

    def set_alert_candidate(
        self,
        key: str,
        state: str,
        *,
        seen: int,
        first_seen: float,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO alert_candidates "
                "(key, state, seen, first_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "state = excluded.state, seen = excluded.seen, "
                "first_seen = excluded.first_seen, updated_at = excluded.updated_at",
                (key, state, seen, first_seen, time.time()),
            )

    def clear_alert_candidate(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM alert_candidates WHERE key = ?", (key,))

    def all_alert_candidate_keys(self) -> list[str]:
        with self._lock:
            return [
                row["key"]
                for row in self.conn.execute(
                    "SELECT key FROM alert_candidates"
                ).fetchall()
            ]

    # ----------------------------------------------------------- alert outbox

    def enqueue_alert(
        self,
        *,
        state_key: str,
        rule: str,
        severity: str,
        title: str,
        detail: str,
        resolve: str = "",
    ) -> int:
        """Кладёт алерт в outbox; повтор того же события не создаёт дубль."""
        with self._lock:
            row = self.conn.execute(
                "SELECT changed_at, notified_at FROM alert_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            changed = float(row["changed_at"]) if row else 0.0
            notified = float(row["notified_at"] or 0) if row else 0.0
            dedupe = f"{state_key}:{severity}:{changed}:{notified}"
            now = time.time()
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO alert_outbox "
                "(dedupe_key, state_key, rule, severity, title, detail, resolve, "
                "created_at, next_attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dedupe,
                    state_key,
                    rule,
                    severity,
                    title,
                    detail,
                    resolve,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0) if cur.rowcount else 0

    def pending_alerts(self, now: float | None = None) -> list[sqlite3.Row]:
        moment = time.time() if now is None else now
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM alert_outbox "
                "WHERE sent_at IS NULL AND next_attempt <= ? ORDER BY id",
                (moment,),
            ).fetchall()

    def mark_alert_sent_row(self, row_id: int, at: float | None = None) -> None:
        moment = time.time() if at is None else at
        with self._lock:
            self.conn.execute(
                "UPDATE alert_outbox SET sent_at = ?, last_error = NULL WHERE id = ?",
                (moment, row_id),
            )

    def retry_alert(self, row_id: int, error: str, next_attempt: float) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE alert_outbox SET attempts = attempts + 1, "
                "next_attempt = ?, last_error = ? WHERE id = ?",
                (next_attempt, error[:500], row_id),
            )

    def prune_alert_outbox(self, older_than: float) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM alert_outbox WHERE sent_at IS NOT NULL AND sent_at < ?",
                (older_than,),
            )

    # --------------------------------------------------------------- bot meta

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM bot_meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_meta(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM bot_meta WHERE key = ?", (key,))

    # ---------------------------------------------------------- screen state

    def get_screen_message(self, chat_id: int) -> int:
        """Возвращает id сообщения-меню, чтобы не плодить его после рестарта."""
        with self._lock:
            row = self.conn.execute(
                "SELECT message_id FROM screen_state WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return int(row["message_id"]) if row else 0

    def set_screen_message(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO screen_state (chat_id, message_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "message_id = excluded.message_id, "
                "updated_at = excluded.updated_at",
                (chat_id, message_id, time.time()),
            )

    # ----------------------------------------------------------------- audit

    def audit(
        self,
        action: str,
        *,
        telegram_id: int | None,
        params: dict[str, Any] | None = None,
        ok: bool = True,
        result: str | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit_log (ts, telegram_id, action, params, ok, result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    telegram_id,
                    action,
                    json.dumps(params, ensure_ascii=False) if params else None,
                    int(ok),
                    result,
                ),
            )

    def prune_audit(self, older_than: float) -> None:
        """Удаляет старые записи журнала: ПДн не должны храниться вечно."""
        with self._lock:
            self.conn.execute("DELETE FROM audit_log WHERE ts < ?", (older_than,))

    # ------------------------------------------------------------ replay guard

    def claim_replay(self, key: str, ttl: float) -> bool:
        """Атомарно занимает одноразовый ключ (TOTP/initData).

        Хранится в SQLite, поэтому повторный код не пройдёт после перезапуска
        процесса и между ботом, Mini App и терминалом (если у них общая база).
        """
        now = time.time()
        expires = now + max(1.0, ttl)
        with self._lock:
            self.conn.execute("DELETE FROM replay_keys WHERE expires <= ?", (now,))
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO replay_keys (key, expires) VALUES (?, ?)",
                (key, expires),
            )
            return cur.rowcount == 1

    def recent_audit(self, limit: int = 15, *, only: str = "") -> list[sqlite3.Row]:
        """Последние записи журнала.

        ``only``: пусто — все, ``denied`` — только отказы, ``actions`` — только
        действия. Разбор инцидента ночью начинается с фильтра, а не с чтения
        всего подряд.
        """
        query = "SELECT * FROM audit_log"
        if only == "denied":
            query += " WHERE action = 'denied'"
        elif only == "actions":
            query += " WHERE action != 'denied'"
        query += " ORDER BY ts DESC LIMIT ?"
        with self._lock:
            return list(self.conn.execute(query, (limit,)).fetchall())

    def mute(self, key: str, seconds: float) -> float:
        """«Знаю, до утра»: заглушить алерт по ключу, вернуть время окончания."""
        until = time.time() + seconds
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO alert_mutes (key, until) VALUES (?, ?)",
                (key, until),
            )
        return until

    def is_muted(self, key: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT until FROM alert_mutes WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return False
            if float(row["until"]) <= time.time():
                self.conn.execute("DELETE FROM alert_mutes WHERE key = ?", (key,))
                return False
            return True

    def prune_mutes(self) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM alert_mutes WHERE until <= ?", (time.time(),)
            )
