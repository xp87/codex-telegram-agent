from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "agent.sqlite"
DEFAULT_LOG_PATH = BASE_DIR / "logs" / "agent.log"
ATTACHMENTS_DIR = BASE_DIR / "data" / "attachments"
PROJECTS_PATH = BASE_DIR / "config" / "projects.json"

logger = logging.getLogger(__name__)


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        escaped = [re.escape(secret) for secret in secrets if secret]
        self._secret_pattern = re.compile("|".join(escaped)) if escaped else None
        self._telegram_token_pattern = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._secret_pattern is not None:
            message = self._secret_pattern.sub("[REDACTED]", message)
        message = self._telegram_token_pattern.sub("[REDACTED_TELEGRAM_TOKEN]", message)
        record.msg = message
        record.args = ()
        return True


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: set[str]
    codex_binary: str
    db_path: Path
    log_path: Path
    max_parallel_jobs: int


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    cwd: Path
    enabled: bool


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    allowed_user_ids = _parse_allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
    max_parallel_jobs = _parse_int(os.getenv("MAX_PARALLEL_JOBS", "1"), default=1)

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        allowed_user_ids=allowed_user_ids,
        codex_binary=os.getenv("CODEX_BINARY", "codex").strip() or "codex",
        db_path=Path(os.getenv("CODEX_AGENT_DB", str(DEFAULT_DB_PATH))).expanduser(),
        log_path=Path(os.getenv("CODEX_AGENT_LOG", str(DEFAULT_LOG_PATH))).expanduser(),
        max_parallel_jobs=max(1, max_parallel_jobs),
    )


def load_projects(config_path: Path = PROJECTS_PATH) -> list[Project]:
    if not config_path.exists():
        raise RuntimeError(f"Не найден config/projects.json: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Некорректный JSON в {config_path}: {exc}") from exc

    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, list):
        raise RuntimeError(f"В {config_path} должен быть список projects")

    seen_ids: set[str] = set()
    projects: list[Project] = []
    for index, item in enumerate(projects_raw, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Проект #{index} в {config_path} должен быть объектом")

        project_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        cwd = str(item.get("cwd", "")).strip()
        enabled = bool(item.get("enabled", True))

        if not project_id or not title or not cwd:
            raise RuntimeError(f"Проект #{index} должен иметь id, title и cwd")
        if project_id in seen_ids:
            raise RuntimeError(f"Дублирующийся project id в {config_path}: {project_id}")

        seen_ids.add(project_id)
        projects.append(Project(id=project_id, title=title, cwd=Path(cwd).expanduser(), enabled=enabled))

    return projects


def validate_startup(settings: Settings, projects: list[Project]) -> str:
    problems: list[str] = []

    if not settings.telegram_bot_token or settings.telegram_bot_token == "put_token_here":
        problems.append("TELEGRAM_BOT_TOKEN не задан. Создай .env на основе .env.example.")

    if not settings.allowed_user_ids:
        problems.append("TELEGRAM_ALLOWED_USER_IDS не задан. Укажи свой Telegram user id.")

    enabled_projects = [project for project in projects if project.enabled]
    if not enabled_projects:
        problems.append("В config/projects.json нет enabled-проектов.")

    for project in enabled_projects:
        if not project.cwd.exists():
            problems.append(f"Не найдена папка проекта {project.title}: {project.cwd}")
        elif not project.cwd.is_dir():
            problems.append(f"cwd проекта {project.title} не является папкой: {project.cwd}")

    try:
        validate_codex_desktop()
    except RuntimeError as exc:
        problems.append(str(exc))

    if problems:
        raise RuntimeError("Проблемы старта:\n- " + "\n- ".join(problems))

    logger.info("Startup config checked: projects=%s codex_desktop=ok", len(enabled_projects))
    return "Codex Desktop"


def validate_codex_desktop() -> None:
    binary = Path("/Applications/Codex.app/Contents/Resources/codex")
    if not binary.exists() or not os.access(binary, os.X_OK):
        raise RuntimeError(f"Codex Desktop app-server не найден: {binary}")
    completed = subprocess.run(
        [str(binary), "app-server", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Codex Desktop app-server недоступен: {error}")


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    redacting_filter = SecretRedactingFilter([os.getenv("TELEGRAM_BOT_TOKEN", "")])
    for handler in handlers:
        handler.addFilter(redacting_filter)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _parse_allowed_user_ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


def _parse_int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
