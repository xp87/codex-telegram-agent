from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Project
from .db import AgentDb
from .telegram_utils import redact_secrets


logger = logging.getLogger(__name__)

SESSIONS_DIR = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class CodexSession:
    session_id: str
    cwd: Path
    title: str
    updated_at: str


def sync_codex_sessions(
    db: AgentDb,
    telegram_user_ids: set[str],
    configured_projects: list[Project],
) -> tuple[list[Project], int]:
    sessions = discover_codex_sessions()
    projects, project_id_by_cwd = merge_projects_with_sessions(configured_projects, sessions)

    imported = 0
    for user_id in telegram_user_ids:
        hidden_session_ids = db.list_hidden_session_ids(user_id)
        for session in sessions:
            if session.session_id in hidden_session_ids:
                continue
            project_id = project_id_by_cwd.get(_resolved_key(session.cwd))
            if project_id is None:
                continue
            if db.upsert_imported_chat(
                user_id,
                project_id,
                session.title,
                session.session_id,
                session.updated_at,
            ):
                imported += 1

    logger.info("Synced Codex sessions: sessions=%s projects=%s imported=%s", len(sessions), len(projects), imported)
    return projects, imported


def discover_codex_sessions(sessions_dir: Path = SESSIONS_DIR) -> list[CodexSession]:
    if not sessions_dir.exists():
        return []

    sessions: list[CodexSession] = []
    for path in sorted(sessions_dir.glob("**/*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        session = _parse_session_file(path)
        if session is None:
            continue
        if not session.cwd.exists() or not session.cwd.is_dir():
            continue
        sessions.append(session)
    return sessions


def merge_projects_with_sessions(
    configured_projects: list[Project],
    sessions: list[CodexSession],
) -> tuple[list[Project], dict[str, str]]:
    projects = [project for project in configured_projects if project.enabled]
    project_id_by_cwd = {_resolved_key(project.cwd): project.id for project in projects}
    used_project_ids = {project.id for project in projects}

    for session in sessions:
        cwd_key = _resolved_key(session.cwd)
        if cwd_key in project_id_by_cwd:
            continue

        project_id = _project_id_for_cwd(session.cwd)
        while project_id in used_project_ids:
            project_id = f"{project_id}_x"

        project = Project(
            id=project_id,
            title=_project_title_for_cwd(session.cwd),
            cwd=session.cwd,
            enabled=True,
        )
        projects.append(project)
        used_project_ids.add(project_id)
        project_id_by_cwd[cwd_key] = project_id

    return projects, project_id_by_cwd


def _parse_session_file(path: Path) -> CodexSession | None:
    meta: dict | None = None
    title_source = ""

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if item.get("type") == "session_meta":
                    meta = item.get("payload") if isinstance(item.get("payload"), dict) else None
                    continue

                if not title_source:
                    title_source = _extract_user_title_source(item)

                if meta is not None and title_source:
                    break
    except OSError:
        return None

    if not meta:
        return None

    session_id = str(meta.get("id") or "").strip()
    cwd = Path(str(meta.get("cwd") or "")).expanduser()
    if not session_id or not str(cwd):
        return None

    timestamp = str(meta.get("timestamp") or "")
    updated_at = _normalize_timestamp(timestamp) or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    title = _make_session_title(updated_at, title_source, path)
    return CodexSession(session_id=session_id, cwd=cwd, title=title, updated_at=updated_at)


def _extract_user_title_source(item: dict) -> str:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return ""
    if item.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
        return ""

    parts = payload.get("content")
    if not isinstance(parts, list):
        return ""

    text = " ".join(
        str(part.get("text") or part.get("input_text") or part.get("output_text") or "")
        for part in parts
        if isinstance(part, dict)
    )
    return _clean_prompt_for_title(text)


def _clean_prompt_for_title(text: str) -> str:
    cleaned = re.sub(r"<INSTRUCTIONS>.*?</INSTRUCTIONS>", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"<environment_context>.*?</environment_context>", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"^# AGENTS\.md instructions for .*$", " ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = redact_secrets(cleaned)
    if not cleaned:
        return ""
    return cleaned[:80].rstrip()


def _make_session_title(updated_at: str, title_source: str, path: Path) -> str:
    date_label = _date_label(updated_at) or path.stem.replace("rollout-", "")[:16]
    if title_source:
        return f"{date_label} · {title_source}"
    return f"{date_label} · Codex session"


def _date_label(timestamp: str) -> str:
    normalized = _normalize_timestamp(timestamp)
    if not normalized:
        return ""
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return normalized[:16]


def _normalize_timestamp(timestamp: str) -> str:
    if not timestamp:
        return ""
    if timestamp.endswith("Z"):
        return timestamp[:-1] + "+00:00"
    return timestamp


def _project_id_for_cwd(cwd: Path) -> str:
    digest = hashlib.sha1(_resolved_key(cwd).encode("utf-8")).hexdigest()[:12]
    return f"codex_{digest}"


def _project_title_for_cwd(cwd: Path) -> str:
    name = cwd.name or cwd.parent.name or str(cwd)
    return name[:60]


def _resolved_key(path: Path) -> str:
    return str(path.expanduser().resolve())
