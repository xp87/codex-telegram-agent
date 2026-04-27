from __future__ import annotations

import json
import logging
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60


class CodexRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexResult:
    output: str
    session_id: str | None


def run_codex_prompt(
    cwd: str,
    prompt: str,
    chat_id: int | None = None,
    codex_session_id: str | None = None,
    attachments: list[dict] | None = None,
) -> str:
    return run_codex_prompt_with_metadata(cwd, prompt, chat_id, codex_session_id, attachments).output


def run_codex_prompt_with_metadata(
    cwd: str,
    prompt: str,
    chat_id: int | None = None,
    codex_session_id: str | None = None,
    attachments: list[dict] | None = None,
) -> CodexResult:
    timeout = int(os.getenv("CODEX_RUN_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise CodexRunError(f"Папка проекта не найдена: {cwd_path}")

    binary = _resolve_codex_binary(os.getenv("CODEX_BINARY", "codex"))
    output_file = tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False)
    output_path = Path(output_file.name)
    output_file.close()

    prepared_prompt = _prompt_with_attachments(prompt, attachments or [])
    image_args = _image_args(attachments or [])
    safe_exec_args = _safe_exec_args()

    args = [
        binary,
        "--ask-for-approval",
        "never",
        "--sandbox",
        "danger-full-access",
        "--cd",
        str(cwd_path),
        "exec",
    ]
    if codex_session_id:
        args.extend(
            [
                "resume",
                *safe_exec_args,
                "--all",
                "--skip-git-repo-check",
                "--json",
                "--output-last-message",
                str(output_path),
                *image_args,
                codex_session_id,
                "-",
            ]
        )
    else:
        args.extend(
            [
                *safe_exec_args,
                "--skip-git-repo-check",
                "--json",
                "--output-last-message",
                str(output_path),
                *image_args,
                "-",
            ]
        )

    started = time.monotonic()
    logger.info(
        "Starting Codex process: cwd=%s chat_id=%s resume=%s attachments=%s images=%s",
        cwd_path,
        chat_id,
        bool(codex_session_id),
        len(attachments or []),
        len(image_args) // 2,
    )
    try:
        completed = subprocess.run(
            args,
            input=prepared_prompt,
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

        if completed.returncode != 0 and _looks_like_tty_error(completed.stdout + completed.stderr):
            logger.info("Codex subprocess requested TTY, retrying with PTY adapter: chat_id=%s", chat_id)
            completed = _run_with_pty(args, prepared_prompt, cwd_path, timeout)

        elapsed = time.monotonic() - started
        logger.info("Finished Codex process: returncode=%s elapsed=%.1fs chat_id=%s", completed.returncode, elapsed, chat_id)

        final_message = _read_text_if_exists(output_path).strip()
        combined_output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
        returned_session_id = _extract_session_id(completed.stdout) or codex_session_id

        if completed.returncode != 0:
            raise CodexRunError(combined_output or f"Codex завершился с кодом {completed.returncode}")

        output = final_message or _extract_last_agent_message(completed.stdout) or "Codex завершил задачу без текстового ответа."
        return CodexResult(output=output, session_id=returned_session_id)
    except subprocess.TimeoutExpired as exc:
        logger.exception("Codex process timeout: cwd=%s chat_id=%s", cwd_path, chat_id)
        raise CodexRunError(f"Codex не завершился за {timeout} секунд") from exc
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary Codex output file: %s", output_path)


def _resolve_codex_binary(value: str) -> str:
    if os.sep in value or (os.altsep and os.altsep in value):
        path = Path(value).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
        raise CodexRunError(f"Codex CLI недоступен: {path}")

    found = shutil.which(value)
    if found:
        return found
    raise CodexRunError(f"Codex CLI не найден: {value}")


def _looks_like_tty_error(text: str) -> bool:
    lowered = text.lower()
    return "tty" in lowered or "terminal" in lowered or "not a typewriter" in lowered


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_exec_args() -> list[str]:
    if os.getenv("CODEX_AGENT_USE_USER_CONFIG", "").lower() in {"1", "true", "yes"}:
        return []

    return [
        "--ignore-user-config",
        "--ignore-rules",
        "--config",
        "mcp_servers={}",
        "--config",
        "plugins={}",
        "--config",
        "apps._default.enabled=false",
        "--config",
        "features.apps=false",
        "--config",
        "features.plugins=false",
    ]


def _prompt_with_attachments(prompt: str, attachments: list[dict]) -> str:
    base = prompt.strip()
    if not attachments:
        return base

    lines = [base, "", "Вложения доступны локально:"]
    for index, attachment in enumerate(attachments, start=1):
        file_path = str(attachment.get("file_path") or "").strip()
        original_name = str(attachment.get("original_file_name") or "").strip()
        mime_type = str(attachment.get("mime_type") or "").strip()
        kind = str(attachment.get("kind") or "file").strip()
        label = original_name or Path(file_path).name
        details = ", ".join(part for part in [kind, mime_type] if part)
        if details:
            lines.append(f"- {index}. {label} ({details}): {file_path}")
        else:
            lines.append(f"- {index}. {label}: {file_path}")
    lines.append("")
    lines.append("Используй эти файлы при выполнении запроса. Изображения также переданы в Codex как image-вложения.")
    return "\n".join(lines).strip()


def _image_args(attachments: list[dict]) -> list[str]:
    args: list[str] = []
    for attachment in attachments:
        file_path = str(attachment.get("file_path") or "").strip()
        if not file_path:
            continue

        mime_type = str(attachment.get("mime_type") or "").lower()
        kind = str(attachment.get("kind") or "").lower()
        is_image = bool(attachment.get("is_image")) or kind == "photo" or mime_type.startswith("image/")
        if not is_image:
            continue

        path = Path(file_path).expanduser()
        if not path.exists() or not path.is_file():
            logger.warning("Image attachment file is missing: %s", path)
            continue
        args.extend(["--image", str(path)])
    return args


def _extract_session_id(output: str) -> str | None:
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            return str(thread_id) if thread_id else None

        if event.get("type") == "session_meta":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            session_id = payload.get("id")
            return str(session_id) if session_id else None

    return None


def _extract_last_agent_message(output: str) -> str:
    message = ""
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "item.completed":
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and item.get("text"):
            message = str(item["text"])

    return message


def _run_with_pty(args: list[str], prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    output_parts: list[bytes] = []
    deadline = time.monotonic() + timeout
    try:
        os.write(master_fd, prompt.encode("utf-8", errors="replace"))
        os.write(master_fd, b"\n\x04")

        while True:
            if time.monotonic() > deadline:
                process.kill()
                raise subprocess.TimeoutExpired(args, timeout)

            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if readable:
                try:
                    data = os.read(master_fd, 8192)
                except OSError:
                    data = b""
                if data:
                    output_parts.append(data)

            if process.poll() is not None:
                break

        while True:
            readable, _, _ = select.select([master_fd], [], [], 0)
            if not readable:
                break
            try:
                data = os.read(master_fd, 8192)
            except OSError:
                break
            if not data:
                break
            output_parts.append(data)
    finally:
        os.close(master_fd)

    stdout = b"".join(output_parts).decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(args=args, returncode=process.returncode or 0, stdout=stdout, stderr="")
