from __future__ import annotations

import re
from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .config import Project


TELEGRAM_MESSAGE_LIMIT = 3500
NAV_PROJECTS = "nav:projects"
NAV_CHATS = "nav:chats"
NAV_STATUS = "nav:status"
NAV_HELP = "nav:help"
NAV_MODEL = "nav:model"
SET_MODEL_PREFIX = "set_model:"
SET_EFFORT_PREFIX = "set_effort:"
BTN_PROJECTS = "Проекты"
BTN_CHATS = "Чаты"
BTN_NEW_CHAT = "Новый чат"
BTN_STATUS = "Статус"
BTN_MODEL = "Модель"
BTN_HELP = "Помощь"
DEFAULT_MODEL_ID = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "medium"
MODEL_OPTIONS = (
    ("gpt-5.5", "GPT-5.5"),
    ("gpt-5.4", "GPT-5.4"),
    ("gpt-5.4-mini", "GPT-5.4 Mini"),
    ("gpt-5.3-codex", "GPT-5.3 Codex"),
    ("gpt-5.3-codex-spark", "Codex Spark"),
    ("gpt-5.2", "GPT-5.2"),
)
EFFORT_OPTIONS = (
    ("low", "Быстро"),
    ("medium", "Средне"),
    ("high", "Глубоко"),
    ("xhigh", "Очень глубоко"),
)

_SECRET_PATTERNS = [
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(
        r"(?i)\b(token|bot_token|api_key|apikey|secret|password|passwd|пароль)\s*([:=])\s*([^\s]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
]


def build_projects_keyboard(projects: Iterable[Project]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(project.title, callback_data=f"project:{project.id}")]
        for project in projects
        if project.enabled
    ]
    rows.append(
        [
            InlineKeyboardButton("Модель", callback_data=NAV_MODEL),
            InlineKeyboardButton("Статус", callback_data=NAV_STATUS),
        ]
    )
    rows.append([InlineKeyboardButton("Помощь", callback_data=NAV_HELP)])
    return InlineKeyboardMarkup(rows)


def build_chats_keyboard(chats: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Новый чат", callback_data="newchat")]]
    rows.extend([InlineKeyboardButton(chat["title"], callback_data=f"chat:{chat['id']}")] for chat in chats)
    rows.append(
        [
            InlineKeyboardButton("Проекты", callback_data=NAV_PROJECTS),
            InlineKeyboardButton("Модель", callback_data=NAV_MODEL),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("Статус", callback_data=NAV_STATUS),
            InlineKeyboardButton("Помощь", callback_data=NAV_HELP),
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_active_chat_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Проекты", callback_data=NAV_PROJECTS),
            InlineKeyboardButton("Чаты", callback_data=NAV_CHATS),
        ],
        [
            InlineKeyboardButton("Новый чат", callback_data="newchat"),
            InlineKeyboardButton("Модель", callback_data=NAV_MODEL),
        ],
        [
            InlineKeyboardButton("Статус", callback_data=NAV_STATUS),
            InlineKeyboardButton("Помощь", callback_data=NAV_HELP),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def build_navigation_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_PROJECTS, BTN_CHATS],
            [BTN_NEW_CHAT, BTN_MODEL],
            [BTN_STATUS, BTN_HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_model_keyboard(active_model: str | None, active_effort: str | None) -> InlineKeyboardMarkup:
    model = active_model or DEFAULT_MODEL_ID
    effort = active_effort or DEFAULT_REASONING_EFFORT
    rows = []
    for index in range(0, len(MODEL_OPTIONS), 2):
        row = []
        for model_id, title in MODEL_OPTIONS[index : index + 2]:
            prefix = "✓ " if model_id == model else ""
            row.append(InlineKeyboardButton(f"{prefix}{title}", callback_data=f"{SET_MODEL_PREFIX}{model_id}"))
        rows.append(row)

    for index in range(0, len(EFFORT_OPTIONS), 2):
        row = []
        for effort_id, title in EFFORT_OPTIONS[index : index + 2]:
            prefix = "✓ " if effort_id == effort else ""
            row.append(InlineKeyboardButton(f"{prefix}{title}", callback_data=f"{SET_EFFORT_PREFIX}{effort_id}"))
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton("Проекты", callback_data=NAV_PROJECTS),
            InlineKeyboardButton("Чаты", callback_data=NAV_CHATS),
        ]
    )
    return InlineKeyboardMarkup(rows)


def model_label(model_id: str | None) -> str:
    model_id = model_id or DEFAULT_MODEL_ID
    return dict(MODEL_OPTIONS).get(model_id, model_id)


def effort_label(effort_id: str | None) -> str:
    effort_id = effort_id or DEFAULT_REASONING_EFFORT
    return dict(EFFORT_OPTIONS).get(effort_id, effort_id)


def split_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def redact_secrets(text: str | None) -> str:
    if not text:
        return ""

    redacted = text
    redacted = _SECRET_PATTERNS[0].sub("[REDACTED_TELEGRAM_TOKEN]", redacted)
    redacted = _SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[2].sub("Bearer [REDACTED]", redacted)
    return redacted
