from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .codex_sessions import sync_codex_sessions
from .config import ATTACHMENTS_DIR, Project, Settings
from .db import AgentDb
from .queue_worker import QueueWorker
from .session_monitor import CodexSessionMonitor, disable_notifications_for_session, enable_notifications_for_session
from .telegram_utils import (
    BTN_CHATS,
    BTN_HELP,
    BTN_MODEL,
    BTN_NEW_CHAT,
    BTN_PROJECTS,
    BTN_STATUS,
    DEFAULT_MODEL_ID,
    DEFAULT_REASONING_EFFORT,
    EFFORT_OPTIONS,
    MODEL_OPTIONS,
    NAV_CHATS,
    NAV_HELP,
    NAV_MODEL,
    NAV_PROJECTS,
    NAV_STATUS,
    SET_EFFORT_PREFIX,
    SET_MODEL_PREFIX,
    build_active_chat_keyboard,
    build_chats_keyboard,
    build_model_keyboard,
    build_navigation_reply_keyboard,
    build_projects_keyboard,
    effort_label,
    model_label,
)


logger = logging.getLogger(__name__)
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TelegramCodexBot:
    def __init__(self, settings: Settings, db: AgentDb, projects: list[Project]):
        self.settings = settings
        self.db = db
        self.configured_projects = [project for project in projects if project.enabled]
        self.projects = list(self.configured_projects)
        self.projects_by_id = {project.id: project for project in self.projects}

    def build_application(self) -> Application:
        builder = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
        )
        application = builder.build()

        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("projects", self.projects_command))
        application.add_handler(CommandHandler("chats", self.chats_command))
        application.add_handler(CommandHandler("newchat", self.newchat_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(CommandHandler("model", self.model_command))
        application.add_handler(CommandHandler("watch", self.watch_command))
        application.add_handler(CommandHandler("unwatch", self.unwatch_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CallbackQueryHandler(self.callback_query))
        application.add_handler(MessageHandler((filters.TEXT | filters.ATTACHMENT) & ~filters.COMMAND, self.text_message))
        return application

    async def _post_init(self, application: Application) -> None:
        self._sync_codex_sessions()
        worker = QueueWorker(self.db, self.projects_by_id, application.bot)
        monitor = CodexSessionMonitor(self.db, self.settings.allowed_user_ids, self.configured_projects, application.bot)
        task = asyncio.create_task(worker.run())
        monitor_task = asyncio.create_task(monitor.run())
        application.bot_data["queue_worker"] = worker
        application.bot_data["queue_worker_task"] = task
        application.bot_data["session_monitor"] = monitor
        application.bot_data["session_monitor_task"] = monitor_task
        logger.info("Telegram bot initialized")

    async def _post_shutdown(self, application: Application) -> None:
        worker = application.bot_data.get("queue_worker")
        task = application.bot_data.get("queue_worker_task")
        monitor = application.bot_data.get("session_monitor")
        monitor_task = application.bot_data.get("session_monitor_task")
        if worker is not None:
            worker.stop()
        if monitor is not None:
            monitor.stop()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
        logger.info("Telegram bot shutdown completed")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await update.effective_message.reply_text("Навигация закреплена снизу.", reply_markup=build_navigation_reply_keyboard())
        await self._show_projects(update)

    async def projects_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await self._show_projects(update)

    async def chats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return

        user_id = self._telegram_user_id(update)
        self._sync_codex_sessions()
        state = self.db.get_user_state(user_id) or {}
        project = self.projects_by_id.get(state.get("active_project_id"))
        if project is None:
            await update.effective_message.reply_text("Сначала выбери проект:", reply_markup=build_projects_keyboard(self.projects))
            return

        await self._show_chats(update, project)

    async def newchat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await self._start_new_chat_flow(update)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await self._send_status(update)

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return

        user_id = self._telegram_user_id(update)
        job = self.db.get_current_job_for_user(user_id)
        if job is None:
            await update.effective_message.reply_text("Нет задач в очереди.")
            return

        if job["status"] == "running":
            await update.effective_message.reply_text(
                f"Задача #{job['id']} уже выполняется. Выполняющаяся задача пока не отменяется."
            )
            return

        if self.db.cancel_job(int(job["id"])):
            logger.info("Job cancelled: id=%s user_id=%s", job["id"], user_id)
            await update.effective_message.reply_text(f"Задача отменена: #{job['id']}")
            return

        await update.effective_message.reply_text("Не удалось отменить задачу.")

    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await self._show_model_settings(update, self._telegram_user_id(update))

    async def watch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return

        user_id = self._telegram_user_id(update)
        chat = self._active_chat(user_id)
        if chat is None:
            await update.effective_message.reply_text("Сначала выбери проект и чат.")
            return
        if not chat.get("codex_session_id"):
            await update.effective_message.reply_text("У этого чата еще нет Codex session id. После первой задачи включу автоматически.")
            return

        enable_notifications_for_session(self.db, user_id, str(chat["codex_session_id"]))
        await update.effective_message.reply_text("Уведомления включены для активного чата.")

    async def unwatch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return

        user_id = self._telegram_user_id(update)
        chat = self._active_chat(user_id)
        if chat is None or not chat.get("codex_session_id"):
            await update.effective_message.reply_text("Сначала выбери чат с Codex session id.")
            return

        disable_notifications_for_session(self.db, user_id, str(chat["codex_session_id"]))
        await update.effective_message.reply_text("Уведомления выключены для активного чата.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await self._send_help(update)

    async def callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        if not self._is_allowed(update):
            with suppress(TelegramError):
                await query.answer("Доступ запрещен.", show_alert=True)
            if query.message:
                await query.message.reply_text("Доступ запрещен.")
            return

        with suppress(TelegramError):
            await query.answer()
        data = query.data or ""
        user_id = self._telegram_user_id(update)
        self._sync_codex_sessions()

        if data == NAV_PROJECTS:
            await self._show_projects(update)
            return

        if data == NAV_CHATS:
            await self._show_active_project_chats(update, user_id)
            return

        if data == NAV_STATUS:
            await self._send_status(update)
            return

        if data == NAV_HELP:
            await self._send_help(update)
            return

        if data == NAV_MODEL:
            await self._show_model_settings(update, user_id)
            return

        if data.startswith(SET_MODEL_PREFIX):
            model_id = data.removeprefix(SET_MODEL_PREFIX)
            if model_id not in dict(MODEL_OPTIONS):
                await query.message.reply_text("Модель недоступна.")
                return
            state = self.db.get_user_state(user_id) or {}
            self.db.update_user_state(
                user_id,
                active_model=model_id,
                active_reasoning_effort=state.get("active_reasoning_effort") or DEFAULT_REASONING_EFFORT,
                mode=None,
            )
            logger.info("Selected model: user_id=%s model=%s", user_id, model_id)
            await self._show_model_settings(update, user_id, prefix="Модель сохранена.")
            return

        if data.startswith(SET_EFFORT_PREFIX):
            effort_id = data.removeprefix(SET_EFFORT_PREFIX)
            if effort_id not in dict(EFFORT_OPTIONS):
                await query.message.reply_text("Скорость недоступна.")
                return
            state = self.db.get_user_state(user_id) or {}
            self.db.update_user_state(
                user_id,
                active_model=state.get("active_model") or DEFAULT_MODEL_ID,
                active_reasoning_effort=effort_id,
                mode=None,
            )
            logger.info("Selected reasoning effort: user_id=%s effort=%s", user_id, effort_id)
            await self._show_model_settings(update, user_id, prefix="Скорость сохранена.")
            return

        if data.startswith("project:"):
            project_id = data.split(":", 1)[1]
            project = self.projects_by_id.get(project_id)
            if project is None:
                await query.message.reply_text("Проект недоступен.")
                return

            self.db.update_user_state(user_id, active_project_id=project.id, active_chat_id=None, mode=None)
            logger.info("Selected project: user_id=%s project_id=%s", user_id, project.id)
            await self._show_chats(update, project)
            return

        if data == "newchat":
            await self._start_new_chat_flow(update)
            return

        if data.startswith("chat:"):
            raw_chat_id = data.split(":", 1)[1]
            try:
                chat_id = int(raw_chat_id)
            except ValueError:
                await query.message.reply_text("Некорректный чат.")
                return

            chat = self.db.get_chat(chat_id, user_id)
            if chat is None:
                await query.message.reply_text("Чат недоступен.")
                return

            project = self.projects_by_id.get(chat["project_id"])
            if project is None:
                await query.message.reply_text("Проект чата недоступен.")
                return

            self.db.update_user_state(user_id, active_project_id=project.id, active_chat_id=chat_id, mode=None)
            if chat.get("codex_session_id"):
                enable_notifications_for_session(self.db, user_id, str(chat["codex_session_id"]))
            logger.info("Selected chat: user_id=%s project_id=%s chat_id=%s", user_id, project.id, chat_id)
            await query.message.reply_text(
                f"Активный проект: {project.title}\nАктивный чат: {chat['title']}\nУведомления включены.\n\nТеперь напиши запрос.",
                reply_markup=build_active_chat_keyboard(),
            )
            return

        await query.message.reply_text("Неизвестная команда.")

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return

        user_id = self._telegram_user_id(update)
        message = update.effective_message
        text = ((message.text or message.caption or "") if message else "").strip()

        self._sync_codex_sessions()
        if text in {BTN_PROJECTS, BTN_CHATS, BTN_NEW_CHAT, BTN_STATUS, BTN_MODEL, BTN_HELP}:
            await self._handle_navigation_button(update, text, user_id)
            return

        state = self.db.get_user_state(user_id) or {}
        if state.get("mode") == "waiting_chat_title":
            if not text:
                await update.effective_message.reply_text("Напиши название чата текстом.")
                return
            await self._create_chat_from_title(update, text, state)
            return

        project = self.projects_by_id.get(state.get("active_project_id"))
        if project is None:
            await update.effective_message.reply_text("Сначала выбери проект:", reply_markup=build_projects_keyboard(self.projects))
            return

        active_chat_id = state.get("active_chat_id")
        chat = self.db.get_chat(int(active_chat_id), user_id) if active_chat_id else None
        if chat is None:
            await self._show_chats(update, project)
            return

        attachments = await self._download_attachments(update, context, user_id)
        if attachments is None:
            return

        if not text and attachments:
            text = "Проанализируй прикрепленные файлы и ответь по ним."
        if not text:
            return

        model_id = state.get("active_model") or DEFAULT_MODEL_ID
        effort_id = state.get("active_reasoning_effort") or DEFAULT_REASONING_EFFORT
        job_id = self.db.create_job(
            user_id,
            project.id,
            int(chat["id"]),
            text,
            model=model_id,
            reasoning_effort=effort_id,
        )
        for attachment in attachments:
            self.db.add_job_attachment(job_id, **attachment)

        logger.info(
            "Job queued: id=%s user_id=%s project_id=%s chat_id=%s attachments=%s",
            job_id,
            user_id,
            project.id,
            chat["id"],
            len(attachments),
        )
        attachment_line = f"\nВложения: {len(attachments)}" if attachments else ""
        await update.effective_message.reply_text(
            f"Задача поставлена в очередь: #{job_id}{attachment_line}\nМодель: {model_label(model_id)} / {effort_label(effort_id)}",
            reply_markup=build_active_chat_keyboard(),
        )

    async def _handle_navigation_button(self, update: Update, text: str, user_id: str) -> None:
        if text == BTN_PROJECTS:
            await self._show_projects(update)
            return
        if text == BTN_CHATS:
            await self._show_active_project_chats(update, user_id)
            return
        if text == BTN_NEW_CHAT:
            await self._start_new_chat_flow(update)
            return
        if text == BTN_STATUS:
            await self._send_status(update)
            return
        if text == BTN_MODEL:
            await self._show_model_settings(update, user_id)
            return
        if text == BTN_HELP:
            await self._send_help(update)
            return

    async def _create_chat_from_title(self, update: Update, title: str, state: dict) -> None:
        user_id = self._telegram_user_id(update)
        project = self.projects_by_id.get(state.get("active_project_id"))
        if project is None:
            self.db.update_user_state(user_id, active_project_id=None, active_chat_id=None, mode=None)
            await update.effective_message.reply_text("Сначала выбери проект:", reply_markup=build_projects_keyboard(self.projects))
            return

        if len(title) > 80:
            await update.effective_message.reply_text("Название слишком длинное. Напиши до 80 символов.")
            return

        chat_id = self.db.create_chat(user_id, project.id, title)
        self.db.update_user_state(user_id, active_project_id=project.id, active_chat_id=chat_id, mode=None)
        logger.info("Chat created: user_id=%s project_id=%s chat_id=%s", user_id, project.id, chat_id)
        await update.effective_message.reply_text(
            f"Активный проект: {project.title}\nАктивный чат: {title}\n\nТеперь напиши запрос.",
            reply_markup=build_active_chat_keyboard(),
        )

    async def _start_new_chat_flow(self, update: Update) -> None:
        user_id = self._telegram_user_id(update)
        self._sync_codex_sessions()
        state = self.db.get_user_state(user_id) or {}
        project = self.projects_by_id.get(state.get("active_project_id"))
        if project is None:
            await update.effective_message.reply_text("Сначала выбери проект:", reply_markup=build_projects_keyboard(self.projects))
            return

        self.db.update_user_state(user_id, active_project_id=project.id, mode="waiting_chat_title")
        await update.effective_message.reply_text("Напиши название чата.")

    async def _show_projects(self, update: Update) -> None:
        self._sync_codex_sessions()
        await update.effective_message.reply_text("Выбери проект:", reply_markup=build_projects_keyboard(self.projects))

    async def _show_chats(self, update: Update, project: Project) -> None:
        user_id = self._telegram_user_id(update)
        self._sync_codex_sessions()
        chats = self.db.list_chats(user_id, project.id)
        if chats:
            text = f"Проект: {project.title}\n\nВыбери чат:"
        else:
            text = f"Проект: {project.title}\n\nЧатов пока нет."

        message = update.effective_message
        if update.callback_query and update.callback_query.message:
            message = update.callback_query.message

        await message.reply_text(text, reply_markup=build_chats_keyboard(chats))

    async def _show_active_project_chats(self, update: Update, user_id: str) -> None:
        state = self.db.get_user_state(user_id) or {}
        project = self.projects_by_id.get(state.get("active_project_id"))
        if project is None:
            await update.effective_message.reply_text("Сначала выбери проект:", reply_markup=build_projects_keyboard(self.projects))
            return
        await self._show_chats(update, project)

    async def _send_status(self, update: Update) -> None:
        user_id = self._telegram_user_id(update)
        self._sync_codex_sessions()
        state = self.db.get_user_state(user_id) or {}
        project = self.projects_by_id.get(state.get("active_project_id"))
        chat = self.db.get_chat(int(state["active_chat_id"]), user_id) if state.get("active_chat_id") else None
        job = self.db.get_latest_job_for_user(user_id)

        lines = [
            f"Активный проект: {project.title if project else '-'}",
            f"Активный чат: {chat['title'] if chat else '-'}",
            f"Модель: {model_label(state.get('active_model'))} / {effort_label(state.get('active_reasoning_effort'))}",
        ]
        if job:
            attachments_count = len(self.db.list_job_attachments(int(job["id"])))
            suffix = f", вложения: {attachments_count}" if attachments_count else ""
            lines.append(f"Последняя задача: #{job['id']} ({job['status']}{suffix})")
        else:
            lines.append("Последняя задача: -")

        await update.effective_message.reply_text("\n".join(lines), reply_markup=build_active_chat_keyboard())

    async def _send_help(self, update: Update) -> None:
        await update.effective_message.reply_text(
            "/start или /projects - выбрать проект\n"
            "/chats - выбрать чат активного проекта\n"
            "/newchat - создать новый чат\n"
            "/status - текущий проект, чат и задача\n"
            "/model - выбрать модель GPT и скорость\n"
            "/cancel - отменить queued задачу\n"
            "/watch - включить уведомления по активному Codex-чату\n"
            "/unwatch - выключить уведомления по активному Codex-чату\n"
            "Можно отправлять текст, фото и документы в активный чат.",
            reply_markup=build_active_chat_keyboard(),
        )

    async def _show_model_settings(self, update: Update, user_id: str, prefix: str | None = None) -> None:
        state = self.db.get_user_state(user_id) or {}
        active_model = state.get("active_model") or DEFAULT_MODEL_ID
        active_effort = state.get("active_reasoning_effort") or DEFAULT_REASONING_EFFORT
        text = (
            f"{prefix + chr(10) if prefix else ''}"
            f"Текущая модель: {model_label(active_model)}\n"
            f"Скорость: {effort_label(active_effort)}\n\n"
            "Выбери модель или скорость:"
        )
        message = update.effective_message
        if update.callback_query and update.callback_query.message:
            message = update.callback_query.message
        await message.reply_text(text, reply_markup=build_model_keyboard(active_model, active_effort))

    async def _download_attachments(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: str,
    ) -> list[dict] | None:
        message = update.effective_message
        if message is None:
            return []

        specs = self._attachment_specs(message)
        if not specs:
            return []

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = ATTACHMENTS_DIR / user_id / f"{timestamp}_{message.message_id}"
        folder.mkdir(parents=True, exist_ok=True)

        attachments: list[dict] = []
        for index, spec in enumerate(specs, start=1):
            safe_name = _safe_filename(spec["file_name"], spec["mime_type"], index)
            path = folder / safe_name
            try:
                telegram_file = await context.bot.get_file(spec["file_id"])
                await telegram_file.download_to_drive(custom_path=str(path))
            except Exception:
                logger.exception("Could not download Telegram attachment: user_id=%s kind=%s", user_id, spec["kind"])
                await message.reply_text("Не удалось скачать вложение. Попробуй еще раз.")
                return None

            attachments.append(
                {
                    "kind": spec["kind"],
                    "file_path": str(path),
                    "original_file_name": spec["original_file_name"],
                    "mime_type": spec["mime_type"],
                    "is_image": spec["is_image"],
                }
            )

        return attachments

    def _attachment_specs(self, message) -> list[dict]:
        specs: list[dict] = []

        if message.photo:
            photo = message.photo[-1]
            specs.append(
                {
                    "kind": "photo",
                    "file_id": photo.file_id,
                    "file_name": f"photo_{message.message_id}.jpg",
                    "original_file_name": None,
                    "mime_type": "image/jpeg",
                    "is_image": True,
                }
            )

        media_fields = [
            ("document", getattr(message, "document", None), "file_name", "mime_type"),
            ("video", getattr(message, "video", None), "file_name", "mime_type"),
            ("audio", getattr(message, "audio", None), "file_name", "mime_type"),
            ("voice", getattr(message, "voice", None), None, "mime_type"),
            ("animation", getattr(message, "animation", None), "file_name", "mime_type"),
            ("video_note", getattr(message, "video_note", None), None, None),
        ]
        for kind, item, file_name_attr, mime_attr in media_fields:
            if item is None:
                continue
            file_name = getattr(item, file_name_attr, None) if file_name_attr else None
            mime_type = getattr(item, mime_attr, None) if mime_attr else None
            fallback_name = file_name or f"{kind}_{message.message_id}{_extension_for_mime(mime_type)}"
            specs.append(
                {
                    "kind": kind,
                    "file_id": item.file_id,
                    "file_name": fallback_name,
                    "original_file_name": file_name,
                    "mime_type": mime_type,
                    "is_image": kind == "document" and str(mime_type or "").lower().startswith("image/"),
                }
            )

        return specs

    async def _ensure_allowed(self, update: Update) -> bool:
        if self._is_allowed(update):
            return True
        if update.effective_message:
            await update.effective_message.reply_text("Доступ запрещен.")
        return False

    def _is_allowed(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        return str(user.id) in self.settings.allowed_user_ids

    def _telegram_user_id(self, update: Update) -> str:
        user = update.effective_user
        if user is None:
            raise TelegramError("No Telegram user in update")
        return str(user.id)

    def _active_chat(self, telegram_user_id: str) -> dict | None:
        state = self.db.get_user_state(telegram_user_id) or {}
        active_chat_id = state.get("active_chat_id")
        if not active_chat_id:
            return None
        return self.db.get_chat(int(active_chat_id), telegram_user_id)

    def _sync_codex_sessions(self) -> None:
        projects, _ = sync_codex_sessions(self.db, self.settings.allowed_user_ids, self.configured_projects)
        self.projects = projects
        self.projects_by_id.clear()
        self.projects_by_id.update({project.id: project for project in projects})


def _safe_filename(name: str | None, mime_type: str | None, index: int) -> str:
    raw_name = Path(name or f"attachment_{index}{_extension_for_mime(mime_type)}").name
    cleaned = _SAFE_FILENAME_RE.sub("_", raw_name).strip("._")
    if not cleaned:
        cleaned = f"attachment_{index}{_extension_for_mime(mime_type)}"
    return f"{index}_{cleaned}"


def _extension_for_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ".bin"
    extension = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip())
    return extension or ".bin"
