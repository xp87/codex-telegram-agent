from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot

from .codex_sessions import SESSIONS_DIR, sync_codex_sessions
from .config import Project
from .db import AgentDb
from .telegram_utils import redact_secrets, split_telegram_text


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionCompletion:
    session_id: str
    completion_key: str
    answer: str
    path: Path


class CodexSessionMonitor:
    def __init__(
        self,
        db: AgentDb,
        telegram_user_ids: set[str],
        configured_projects: list[Project],
        bot: Bot,
        poll_interval: float = 5.0,
    ):
        self.db = db
        self.telegram_user_ids = telegram_user_ids
        self.configured_projects = configured_projects
        self.bot = bot
        self.poll_interval = poll_interval
        self.projects_by_id: dict[str, Project] = {project.id: project for project in configured_projects if project.enabled}
        self._last_sync_monotonic = 0.0
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        logger.info("Codex session monitor started")
        try:
            self._sync_sessions()
            self._mark_missing_watches_seen()

            while not self._stop_event.is_set():
                await self._poll_once()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
        finally:
            logger.info("Codex session monitor stopped")

    async def _poll_once(self) -> None:
        self._sync_sessions_if_needed()

        for chat in self.db.list_watched_session_chats():
            session_id = str(chat.get("codex_session_id") or "")
            if not session_id:
                continue

            completion = latest_completion_for_session_id(session_id)
            if completion is None or not completion.answer:
                continue

            if chat.get("last_completion_key") == completion.completion_key:
                continue

            text = redact_secrets(
                f"Проект: {self._project_title_for_chat(chat)}\n"
                f"Чат: {chat['title']}\n\n"
                f"{completion.answer}"
            )
            for chunk in split_telegram_text(text):
                await self.bot.send_message(chat_id=str(chat["telegram_user_id"]), text=chunk)

            self.db.set_session_notification_key(
                str(chat["telegram_user_id"]),
                session_id,
                completion.completion_key,
            )
            logger.info("Sent external Codex completion notification: session_id=%s chat_id=%s", session_id, chat["id"])

    def _sync_sessions(self) -> None:
        projects, _ = sync_codex_sessions(self.db, self.telegram_user_ids, self.configured_projects)
        self.projects_by_id = {project.id: project for project in projects}
        self._last_sync_monotonic = asyncio.get_running_loop().time()

    def _sync_sessions_if_needed(self) -> None:
        if asyncio.get_running_loop().time() - self._last_sync_monotonic < 60:
            return
        self._sync_sessions()

    def _project_title_for_chat(self, chat: dict) -> str:
        project = self.projects_by_id.get(str(chat.get("project_id") or ""))
        if project is not None:
            return project.title
        return "Неизвестный проект"

    def _mark_missing_watches_seen(self) -> None:
        completions = latest_completions_by_session_id()
        for chat in self.db.list_watched_session_chats():
            if chat.get("last_completion_key"):
                continue

            session_id = str(chat.get("codex_session_id") or "")
            completion = completions.get(session_id)
            if completion is None:
                continue

            self.db.set_session_notification_key(
                str(chat["telegram_user_id"]),
                session_id,
                completion.completion_key,
            )


def enable_notifications_for_session(db: AgentDb, telegram_user_id: str, codex_session_id: str) -> None:
    completion = latest_completion_for_session_id(codex_session_id)
    db.set_session_notifications(
        telegram_user_id,
        codex_session_id,
        enabled=True,
        last_completion_key=completion.completion_key if completion else None,
    )


def disable_notifications_for_session(db: AgentDb, telegram_user_id: str, codex_session_id: str) -> None:
    db.set_session_notifications(telegram_user_id, codex_session_id, enabled=False)


def mark_latest_completion_seen(db: AgentDb, telegram_user_id: str, codex_session_id: str) -> None:
    completion = latest_completion_for_session_id(codex_session_id)
    if completion is None:
        return

    db.set_session_notifications(
        telegram_user_id,
        codex_session_id,
        enabled=True,
        last_completion_key=completion.completion_key,
    )


def latest_completion_for_session_id(codex_session_id: str) -> SessionCompletion | None:
    if not codex_session_id:
        return None

    for path in SESSIONS_DIR.glob(f"**/*{codex_session_id}.jsonl"):
        completion = latest_completion_from_file(path)
        if completion is not None:
            return completion
    return None


def latest_completions_by_session_id() -> dict[str, SessionCompletion]:
    completions: dict[str, SessionCompletion] = {}
    if not SESSIONS_DIR.exists():
        return completions

    for path in SESSIONS_DIR.glob("**/*.jsonl"):
        completion = latest_completion_from_file(path)
        if completion is not None:
            completions[completion.session_id] = completion
    return completions


def latest_completion_from_file(path: Path) -> SessionCompletion | None:
    session_id = ""
    last_agent_message = ""
    latest_completion: SessionCompletion | None = None

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if item.get("type") == "session_meta":
                    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                    session_id = str(payload.get("id") or "")
                    continue

                last_agent_message = _update_last_agent_message(item, last_agent_message)

                if item.get("type") != "event_msg":
                    continue
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                if payload.get("type") != "task_complete":
                    continue

                answer = str(payload.get("last_agent_message") or last_agent_message).strip()
                if not session_id or not answer:
                    continue

                turn_id = str(payload.get("turn_id") or item.get("timestamp") or line_no)
                answer_hash = hashlib.sha1(answer.encode("utf-8", errors="replace")).hexdigest()[:12]
                latest_completion = SessionCompletion(
                    session_id=session_id,
                    completion_key=f"{turn_id}:{answer_hash}",
                    answer=answer,
                    path=path,
                )
    except OSError:
        return None

    return latest_completion


def _update_last_agent_message(item: dict, current: str) -> str:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return current

    if item.get("type") == "event_msg" and payload.get("type") == "agent_message":
        return str(payload.get("message") or current).strip()

    if item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
        content = payload.get("content")
        if not isinstance(content, list):
            return current
        text = " ".join(
            str(part.get("text") or part.get("output_text") or "")
            for part in content
            if isinstance(part, dict)
        ).strip()
        return text or current

    if item.get("type") == "item.completed":
        completed_item = item.get("item") if isinstance(item.get("item"), dict) else {}
        if completed_item.get("type") == "agent_message" and completed_item.get("text"):
            return str(completed_item["text"]).strip()

    return current
