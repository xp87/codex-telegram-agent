from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_UNSET = object()


class AgentDb:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  telegram_user_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  codex_session_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chats_user_project
                  ON chats (telegram_user_id, project_id);

                CREATE INDEX IF NOT EXISTS idx_chats_user_session
                  ON chats (telegram_user_id, codex_session_id);

                CREATE TABLE IF NOT EXISTS user_state (
                  telegram_user_id TEXT PRIMARY KEY,
                  active_project_id TEXT,
                  active_chat_id INTEGER,
                  active_model TEXT,
                  active_reasoning_effort TEXT,
                  mode TEXT,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  telegram_user_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  chat_id INTEGER NOT NULL,
                  prompt TEXT NOT NULL,
                  model TEXT,
                  reasoning_effort TEXT,
                  status TEXT NOT NULL,
                  result TEXT,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS job_attachments (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  file_path TEXT NOT NULL,
                  original_file_name TEXT,
                  mime_type TEXT,
                  is_image INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_job_attachments_job
                  ON job_attachments (job_id);

                CREATE TABLE IF NOT EXISTS codex_session_notifications (
                  telegram_user_id TEXT NOT NULL,
                  codex_session_id TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 0,
                  last_completion_key TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (telegram_user_id, codex_session_id)
                );

                CREATE TABLE IF NOT EXISTS hidden_codex_sessions (
                  telegram_user_id TEXT NOT NULL,
                  codex_session_id TEXT NOT NULL,
                  hidden_at TEXT NOT NULL,
                  PRIMARY KEY (telegram_user_id, codex_session_id)
                );
                """
            )
            self._ensure_column_unlocked("user_state", "active_model", "TEXT")
            self._ensure_column_unlocked("user_state", "active_reasoning_effort", "TEXT")
            self._ensure_column_unlocked("jobs", "model", "TEXT")
            self._ensure_column_unlocked("jobs", "reasoning_effort", "TEXT")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_user_state(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM user_state WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_dict(row)

    def update_user_state(
        self,
        telegram_user_id: str,
        *,
        active_project_id: str | None | object = _UNSET,
        active_chat_id: int | None | object = _UNSET,
        active_model: str | None | object = _UNSET,
        active_reasoning_effort: str | None | object = _UNSET,
        mode: str | None | object = _UNSET,
    ) -> None:
        current = self.get_user_state(telegram_user_id) or {}
        next_project_id = current.get("active_project_id") if active_project_id is _UNSET else active_project_id
        next_chat_id = current.get("active_chat_id") if active_chat_id is _UNSET else active_chat_id
        next_model = current.get("active_model") if active_model is _UNSET else active_model
        next_effort = (
            current.get("active_reasoning_effort")
            if active_reasoning_effort is _UNSET
            else active_reasoning_effort
        )
        next_mode = current.get("mode") if mode is _UNSET else mode

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_state
                  (telegram_user_id, active_project_id, active_chat_id, active_model, active_reasoning_effort, mode, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                  active_project_id = excluded.active_project_id,
                  active_chat_id = excluded.active_chat_id,
                  active_model = excluded.active_model,
                  active_reasoning_effort = excluded.active_reasoning_effort,
                  mode = excluded.mode,
                  updated_at = excluded.updated_at
                """,
                (telegram_user_id, next_project_id, next_chat_id, next_model, next_effort, next_mode, now_iso()),
            )
            self._conn.commit()

    def list_chats(self, telegram_user_id: str, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM chats
                WHERE telegram_user_id = ? AND project_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (telegram_user_id, project_id),
            ).fetchall()
            return [_row_to_dict(row) for row in rows]

    def create_chat(self, telegram_user_id: str, project_id: str, title: str) -> int:
        timestamp = now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO chats (telegram_user_id, project_id, title, codex_session_id, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (telegram_user_id, project_id, title, timestamp, timestamp),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def upsert_imported_chat(
        self,
        telegram_user_id: str,
        project_id: str,
        title: str,
        codex_session_id: str,
        updated_at: str,
    ) -> bool:
        timestamp = updated_at or now_iso()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT id FROM chats
                WHERE telegram_user_id = ? AND codex_session_id = ?
                """,
                (telegram_user_id, codex_session_id),
            ).fetchone()

            if existing:
                self._conn.execute(
                    """
                    UPDATE chats
                    SET project_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (project_id, timestamp, int(existing["id"])),
                )
                self._conn.commit()
                return False

            self._conn.execute(
                """
                INSERT INTO chats
                  (telegram_user_id, project_id, title, codex_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_user_id, project_id, title, codex_session_id, timestamp, timestamp),
            )
            self._conn.commit()
            return True

    def get_chat(self, chat_id: int, telegram_user_id: str | None = None) -> dict[str, Any] | None:
        if telegram_user_id is None:
            query = "SELECT * FROM chats WHERE id = ?"
            params: tuple[Any, ...] = (chat_id,)
        else:
            query = "SELECT * FROM chats WHERE id = ? AND telegram_user_id = ?"
            params = (chat_id, telegram_user_id)

        with self._lock:
            row = self._conn.execute(query, params).fetchone()
            return _row_to_dict(row)

    def touch_chat(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now_iso(), chat_id))
            self._conn.commit()

    def set_chat_session_id(self, chat_id: int, codex_session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE chats
                SET codex_session_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (codex_session_id, now_iso(), chat_id),
            )
            self._conn.commit()

    def list_watched_session_chats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                  c.*,
                  n.last_completion_key AS last_completion_key,
                  n.enabled AS notifications_enabled
                FROM chats c
                JOIN codex_session_notifications n
                  ON n.telegram_user_id = c.telegram_user_id
                 AND n.codex_session_id = c.codex_session_id
                WHERE c.codex_session_id IS NOT NULL
                  AND n.enabled = 1
                ORDER BY c.updated_at DESC, c.id DESC
                """
            ).fetchall()
            return [_row_to_dict(row) for row in rows]

    def list_hidden_session_ids(self, telegram_user_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT codex_session_id FROM hidden_codex_sessions
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchall()
            return {str(row["codex_session_id"]) for row in rows}

    def hide_codex_sessions(self, telegram_user_id: str, session_ids: list[str]) -> int:
        cleaned = [session_id.strip() for session_id in session_ids if session_id and session_id.strip()]
        if not cleaned:
            return 0

        timestamp = now_iso()
        with self._lock:
            for session_id in cleaned:
                self._conn.execute(
                    """
                    INSERT INTO hidden_codex_sessions (telegram_user_id, codex_session_id, hidden_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(telegram_user_id, codex_session_id) DO UPDATE SET
                      hidden_at = excluded.hidden_at
                    """,
                    (telegram_user_id, session_id, timestamp),
                )
            self._conn.commit()
            return len(cleaned)

    def set_session_notifications(
        self,
        telegram_user_id: str,
        codex_session_id: str,
        *,
        enabled: bool,
        last_completion_key: str | None | object = _UNSET,
    ) -> None:
        current = self.get_session_notification(telegram_user_id, codex_session_id) or {}
        next_last_key = current.get("last_completion_key") if last_completion_key is _UNSET else last_completion_key

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO codex_session_notifications
                  (telegram_user_id, codex_session_id, enabled, last_completion_key, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id, codex_session_id) DO UPDATE SET
                  enabled = excluded.enabled,
                  last_completion_key = excluded.last_completion_key,
                  updated_at = excluded.updated_at
                """,
                (telegram_user_id, codex_session_id, 1 if enabled else 0, next_last_key, now_iso()),
            )
            self._conn.commit()

    def get_session_notification(self, telegram_user_id: str, codex_session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM codex_session_notifications
                WHERE telegram_user_id = ? AND codex_session_id = ?
                """,
                (telegram_user_id, codex_session_id),
            ).fetchone()
            return _row_to_dict(row)

    def set_session_notification_key(
        self,
        telegram_user_id: str,
        codex_session_id: str,
        last_completion_key: str,
    ) -> None:
        self.set_session_notifications(
            telegram_user_id,
            codex_session_id,
            enabled=True,
            last_completion_key=last_completion_key,
        )

    def create_job(
        self,
        telegram_user_id: str,
        project_id: str,
        chat_id: int,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO jobs
                  (telegram_user_id, project_id, chat_id, prompt, model, reasoning_effort, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (telegram_user_id, project_id, chat_id, prompt, model, reasoning_effort, now_iso()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def add_job_attachment(
        self,
        job_id: int,
        *,
        kind: str,
        file_path: str,
        original_file_name: str | None,
        mime_type: str | None,
        is_image: bool,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO job_attachments
                  (job_id, kind, file_path, original_file_name, mime_type, is_image, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    file_path,
                    original_file_name,
                    mime_type,
                    1 if is_image else 0,
                    now_iso(),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_job_attachments(self, job_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM job_attachments
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()
            return [_row_to_dict(row) for row in rows]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _row_to_dict(row)

    def get_latest_job_for_user(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE telegram_user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_user_id,),
            ).fetchone()
            return _row_to_dict(row)

    def get_current_job_for_user(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE telegram_user_id = ? AND status IN ('running', 'queued')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id DESC
                LIMIT 1
                """,
                (telegram_user_id,),
            ).fetchone()
            return _row_to_dict(row)

    def claim_next_queued_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued'
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None

            job_id = int(row["id"])
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_iso(), job_id),
            )
            self._conn.commit()

            updated = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _row_to_dict(updated)

    def finish_job(self, job_id: int, status: str, *, result: str | None = None, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, result = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, result, error, now_iso(), job_id),
            )
            self._conn.commit()

    def fail_running_jobs_on_startup(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = 'Агент был перезапущен во время выполнения задачи. Отправь запрос еще раз.',
                    finished_at = ?
                WHERE status = 'running'
                """,
                (now_iso(),),
            )
            self._conn.commit()
            return cursor.rowcount

    def cancel_job(self, job_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', finished_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_iso(), job_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def _ensure_column_unlocked(self, table: str, column: str, definition: str) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in existing:
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)
