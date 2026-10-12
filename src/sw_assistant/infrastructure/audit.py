from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..domain import utc_now


_SENSITIVE = re.compile(r"(authorization|api[_-]?key|token|secret|password)", re.I)


def _redact(value: Any, key: str = "") -> Any:
    if _SENSITIVE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return "<redacted>"
    return value


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **payload: Any) -> None:
        record = {"event": event, "timestamp": utc_now(), **_redact(payload)}
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")

    @staticmethod
    def prune(
        directory: str | Path,
        retention_days: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, list[str]]:
        """Delete expired session logs while leaving unrelated files untouched."""
        root = Path(directory)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(days=max(1, retention_days))
        result: dict[str, list[str]] = {"deleted": [], "errors": []}
        try:
            candidates = tuple(root.glob("session-*.jsonl"))
        except OSError as exc:
            result["errors"].append(str(exc))
            return result
        for path in candidates:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified >= cutoff:
                    continue
                path.unlink()
                result["deleted"].append(path.name)
            except OSError as exc:
                result["errors"].append(f"{path.name}: {exc}")
        return result
