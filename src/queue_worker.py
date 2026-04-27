from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from telegram import Bot

from .codex_desktop import refresh_codex_desktop_thread, run_codex_desktop_prompt
from .config import Project
from .db import AgentDb
from .session_monitor import latest_completion_for_session_id, mark_latest_completion_seen
from .telegram_utils import effort_label, model_label, redact_secrets, split_telegram_text


logger = logging.getLogger(__name__)


class QueueWorker:
    def __init__(self, db: AgentDb, projects_by_id: dict[str, Project], bot: Bot, poll_interval: float = 2.0):
        self.db = db
        self.projects_by_id = projects_by_id
        self.bot = bot
        self.poll_interval = poll_interval
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        logger.info("Queue worker started")
        try:
            while not self._stop_event.is_set():
                job = self.db.claim_next_queued_job()
                if job is None:
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
                    continue

                await self._process_job(job)
        finally:
            logger.info("Queue worker stopped")

    async def _process_job(self, job: dict) -> None:
        job_id = int(job["id"])
        telegram_user_id = str(job["telegram_user_id"])
        project = self.projects_by_id.get(job["project_id"])
        chat = self.db.get_chat(int(job["chat_id"]), telegram_user_id)

        if project is None or not project.enabled:
            error = f"Проект недоступен: {job['project_id']}"
            self.db.finish_job(job_id, "failed", error=error)
            await self._send_message(telegram_user_id, f"Ошибка задачи #{job_id}:\n{error}")
            return

        if chat is None:
            error = f"Чат недоступен: {job['chat_id']}"
            self.db.finish_job(job_id, "failed", error=error)
            await self._send_message(telegram_user_id, f"Ошибка задачи #{job_id}:\n{error}")
            return

        attachments = self.db.list_job_attachments(job_id)
        attachments_line = f"\nВложения: {len(attachments)}" if attachments else ""
        model_id = str(job.get("model") or "").strip() or None
        effort_id = str(job.get("reasoning_effort") or "").strip() or None
        model_line = f"\nМодель: {model_label(model_id)} / {effort_label(effort_id)}"
        logger.info("Job started: id=%s project_id=%s chat_id=%s", job_id, project.id, chat["id"])
        await self._send_message(
            telegram_user_id,
            f"В работе в Codex Desktop: #{job_id}\nПроект: {project.title}\nЧат: {chat['title']}{model_line}{attachments_line}",
        )

        session_id = str(chat.get("codex_session_id") or "").strip() or None
        baseline_completion = latest_completion_for_session_id(session_id)
        baseline_key = baseline_completion.completion_key if baseline_completion else None
        if session_id:
            self.db.set_session_notifications(
                telegram_user_id,
                session_id,
                enabled=False,
                last_completion_key=baseline_key,
            )

        try:
            codex_result = await asyncio.to_thread(
                run_codex_desktop_prompt,
                str(project.cwd),
                str(job["prompt"]),
                session_id=session_id,
                chat_title=str(chat["title"]),
                attachments=attachments,
                model=model_id,
                reasoning_effort=effort_id,
            )
        except Exception as exc:
            error = redact_secrets(str(exc))
            self.db.finish_job(job_id, "failed", error=error)
            if session_id:
                self.db.set_session_notifications(
                    telegram_user_id,
                    session_id,
                    enabled=True,
                    last_completion_key=baseline_key,
                )
            logger.exception("Job failed: id=%s project_id=%s chat_id=%s", job_id, project.id, chat["id"])
            await self._send_message(telegram_user_id, f"Ошибка задачи #{job_id}:\n{error}")
            return

        if codex_result.session_id != chat.get("codex_session_id"):
            self.db.set_chat_session_id(int(chat["id"]), codex_result.session_id)

        mark_latest_completion_seen(self.db, telegram_user_id, codex_result.session_id)
        refresh_codex_desktop_thread(codex_result.session_id)
        result = redact_secrets(
            f"Проект: {project.title}\n"
            f"Чат: {chat['title']}\n\n"
            f"Модель: {model_label(model_id)} / {effort_label(effort_id)}\n\n"
            f"{codex_result.output}"
        )
        self.db.finish_job(job_id, "done", result=result)
        self.db.touch_chat(int(chat["id"]))
        logger.info("Job done: id=%s project_id=%s chat_id=%s", job_id, project.id, chat["id"])
        await self._send_message(
            telegram_user_id,
            f"Готово: #{job_id}\n\n{result}",
        )

    async def _send_message(self, telegram_user_id: str, text: str) -> None:
        for chunk in split_telegram_text(text):
            await self.bot.send_message(chat_id=telegram_user_id, text=chunk)
