from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60


class CodexDesktopError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexDesktopResult:
    session_id: str
    turn_id: str
    output: str


def run_codex_desktop_prompt(
    cwd: str,
    prompt: str,
    *,
    session_id: str | None = None,
    chat_title: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> CodexDesktopResult:
    prompt = prompt.strip()
    if not prompt:
        raise CodexDesktopError("Пустой запрос не отправлен в Codex Desktop.")

    timeout_seconds = _timeout_seconds()
    binary = _codex_desktop_binary()
    attachments = attachments or []
    logger.info(
        "Starting Codex Desktop app-server turn: cwd=%s session_id=%s model=%s effort=%s attachments=%s",
        cwd,
        session_id or "",
        model or "",
        reasoning_effort or "",
        len(attachments),
    )

    process = subprocess.Popen(
        [str(binary), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=_app_server_env(),
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise CodexDesktopError("Не удалось открыть stdio для Codex Desktop app-server.")

    try:
        client = _JsonLineClient(process)
        client.send("initialize", {
            "clientInfo": {
                "name": "codex-telegram-agent",
                "title": "Codex Telegram Agent",
                "version": "0.1",
            },
            "capabilities": {
                "experimentalApi": True,
                "optOutNotificationMethods": [
                    "item/agentMessage/delta",
                    "item/reasoning/textDelta",
                    "item/reasoning/summaryTextDelta",
                    "thread/tokenUsage/updated",
                    "account/rateLimits/updated",
                    "mcpServer/startupStatus/updated",
                    "skills/changed",
                ],
            },
        })

        active_session_id = session_id
        if active_session_id:
            resume_params = {
                "threadId": active_session_id,
                "cwd": cwd,
                "excludeTurns": True,
                "persistExtendedHistory": True,
            }
            if model:
                resume_params["model"] = model
            client.send("thread/resume", resume_params)
        else:
            start_params = {
                "cwd": cwd,
                "experimentalRawEvents": False,
                "persistExtendedHistory": True,
            }
            if model:
                start_params["model"] = model
            client.send("thread/start", start_params)

        title_request_id: int | None = None
        start_request_id: int | None = None
        turn_id = ""
        final_answer = ""
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            item = client.read_next(deadline)
            if item is None:
                continue

            if "error" in item:
                raise CodexDesktopError(_format_rpc_error(item))

            if item.get("id") == 2 and "result" in item:
                thread = item["result"].get("thread") if isinstance(item["result"], dict) else {}
                new_session_id = str(thread.get("id") or "")
                if new_session_id:
                    active_session_id = new_session_id
                    if chat_title:
                        title_request_id = client.send("thread/name/set", {
                            "threadId": active_session_id,
                            "name": chat_title[:120],
                        })
                    turn_params = {
                        "threadId": active_session_id,
                        "input": _build_user_input(prompt, attachments),
                        "cwd": cwd,
                    }
                    if model:
                        turn_params["model"] = model
                    if reasoning_effort:
                        turn_params["effort"] = reasoning_effort
                    start_request_id = client.send("turn/start", turn_params)
                continue

            if start_request_id is not None and item.get("id") == start_request_id and "result" in item:
                turn = item["result"].get("turn") if isinstance(item["result"], dict) else {}
                turn_id = str(turn.get("id") or turn_id)
                continue

            method = item.get("method")
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            if method == "turn/started":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                turn_id = str(turn.get("id") or turn_id)
                continue

            if method == "item/completed":
                completed = params.get("item") if isinstance(params.get("item"), dict) else {}
                if completed.get("type") == "agentMessage" and completed.get("text"):
                    final_answer = str(completed["text"]).strip()
                continue

            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                turn_id = str(turn.get("id") or turn_id)
                error = turn.get("error")
                if error:
                    raise CodexDesktopError(str(error))
                if not active_session_id:
                    raise CodexDesktopError("Codex Desktop завершил задачу без session id.")
                return CodexDesktopResult(
                    session_id=active_session_id,
                    turn_id=turn_id,
                    output=final_answer or "Codex Desktop завершил задачу без финального текста.",
                )

            if title_request_id is not None and item.get("id") == title_request_id:
                continue

        raise TimeoutError(f"Codex Desktop не вернул итоговый ответ за {timeout_seconds} секунд.")
    finally:
        _terminate_process(process)


def build_desktop_prompt(prompt: str, attachments: list[dict[str, Any]]) -> str:
    parts = [prompt.strip()]
    non_image_attachments = [item for item in attachments if not bool(item.get("is_image"))]
    if non_image_attachments:
        lines = ["", "Вложения доступны локально:"]
        for index, attachment in enumerate(non_image_attachments, start=1):
            file_path = str(attachment.get("file_path") or "")
            original_name = str(attachment.get("original_file_name") or Path(file_path).name)
            mime_type = str(attachment.get("mime_type") or "").strip()
            suffix = f" ({mime_type})" if mime_type else ""
            lines.append(f"{index}. {original_name}{suffix}: {file_path}")
        parts.append("\n".join(lines))
    return "\n\n".join(part for part in parts if part.strip())


def _build_user_input(prompt: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_desktop_prompt(prompt, attachments),
            "text_elements": [],
        }
    ]
    for attachment in attachments:
        if bool(attachment.get("is_image")):
            file_path = str(attachment.get("file_path") or "")
            if file_path:
                items.append({"type": "localImage", "path": file_path})
    return items


class _JsonLineClient:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.next_id = 1
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr_chunks: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def send(self, method: str, params: dict[str, Any] | None) -> int:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        assert self.process.stdin is not None
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        return request_id

    def read_next(self, deadline: float) -> dict[str, Any] | None:
        timeout = max(0.1, min(1.0, deadline - time.monotonic()))
        try:
            item = self.messages.get(timeout=timeout)
        except queue.Empty:
            item = None

        if item is not None:
            return item

        if self.process.poll() is not None:
            stderr = "".join(self.stderr_chunks)[-4000:].strip()
            raise CodexDesktopError(f"Codex Desktop app-server завершился раньше времени: {stderr}")
        return None

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                self.messages.put(json.loads(line.decode("utf-8", errors="replace")))
            except json.JSONDecodeError as exc:
                self.messages.put({
                    "error": {
                        "code": "invalid_json",
                        "message": f"Некорректный ответ Codex Desktop app-server: {line[:200]!r}",
                    }
                })
        self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw_line in self.process.stderr:
            self.stderr_chunks.append(raw_line.decode("utf-8", errors="replace"))
            if len(self.stderr_chunks) > 80:
                del self.stderr_chunks[:40]


def _codex_desktop_binary() -> Path:
    bundled = Path("/Applications/Codex.app/Contents/Resources/codex")
    if bundled.exists() and os.access(bundled, os.X_OK):
        return bundled
    configured = os.getenv("CODEX_BINARY", "codex").strip() or "codex"
    return Path(configured)


def _app_server_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_SANDBOX_NETWORK_DISABLED"] = env.get("CODEX_SANDBOX_NETWORK_DISABLED", "0")
    return env


def refresh_codex_desktop_thread(session_id: str) -> None:
    if os.getenv("CODEX_DESKTOP_BACKGROUND_REFRESH", "1").strip() in {"0", "false", "False", "no"}:
        return
    if not session_id:
        return
    subprocess.run(
        ["open", "-g", f"codex://threads/{quote(session_id, safe='')}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _format_rpc_error(item: dict[str, Any]) -> str:
    error = item.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error)
        code = error.get("code")
        return f"Codex Desktop app-server error {code}: {message}"
    return f"Codex Desktop app-server error: {error}"


def _timeout_seconds() -> int:
    raw = os.getenv("CODEX_DESKTOP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(30, value)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
