"""Atomic local task snapshots. No provider configuration or credentials are stored."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any


_SECRET = re.compile(r"(?i)\b(api[ _-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,;；]+")
_SECRET_KEYS = ("api_key", "authorization", "password", "secret", "token")


def _sanitize(value: Any, key: str = "") -> Any:
    if any(marker in key.casefold() for marker in _SECRET_KEYS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub(r"\1=<redacted>", value)
    return value


class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _file(self, task_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", task_id):
            raise ValueError("Invalid task id")
        return self.path / f"{task_id}.json"

    def save(self, task: dict[str, Any]) -> None:
        destination = self._file(str(task["id"]))
        payload = json.dumps(_sanitize(task), ensure_ascii=False, indent=2, allow_nan=False)
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with self._lock:
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def load(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                value = json.loads(self._file(task_id).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
        if not isinstance(value, dict) or value.get("id") != task_id:
            raise ValueError("Invalid task snapshot")
        return value

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        with self._lock:
            files = sorted(self.path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[:max(0, min(limit, 1000))]:
                try:
                    task = self.load(path.stem)
                    if task is None:
                        continue
                    values.append({key: task.get(key) for key in (
                        "id", "prompt", "status", "phase", "message", "updated_at", "rounds",
                    )})
                except (OSError, ValueError):
                    continue
        return values

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.list_tasks(limit)
