from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class AddinEvent:
    schema_version: int
    sequence: int
    session_id: str
    event_id: str
    timestamp_utc: str
    process_id: int
    name: str
    payload: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AddinEvent":
        schema = int(value.get("schema_version") or 0)
        name = str(value.get("name") or "").strip()
        event_id = str(value.get("event_id") or "").strip()
        if schema != 1:
            raise ValueError(f"不支持的 Add-in 事件协议版本：{schema}")
        if not name or not event_id:
            raise ValueError("Add-in 事件缺少 name 或 event_id。")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            schema_version=schema,
            sequence=int(value.get("sequence") or 0),
            session_id=str(value.get("session_id") or ""),
            event_id=event_id,
            timestamp_utc=str(value.get("timestamp_utc") or ""),
            process_id=int(value.get("process_id") or 0),
            name=name,
            payload=dict(payload),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "timestamp_utc": self.timestamp_utc,
            "process_id": self.process_id,
            "name": self.name,
            "payload": dict(self.payload),
        }


class EventBridgeMonitor:
    """Tails the durable Add-in JSONL channel without replaying old sessions."""

    def __init__(
        self,
        events_path: str | Path,
        on_event: Callable[[AddinEvent], None],
        *,
        poll_seconds: float = 0.2,
        start_at_end: bool = True,
    ) -> None:
        self.events_path = Path(events_path)
        self.on_event = on_event
        self.poll_seconds = max(0.02, poll_seconds)
        self.start_at_end = start_at_end
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        if self.start_at_end and self.events_path.is_file():
            self._offset = self.events_path.stat().st_size
        else:
            self._offset = 0
        self._thread = threading.Thread(
            target=self._run,
            name="SolidWorksAddinEventMonitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        self._thread = None

    def poll_once(self) -> int:
        try:
            size = self.events_path.stat().st_size
        except FileNotFoundError:
            return 0
        if size < self._offset:
            self._offset = 0

        delivered = 0
        with self.events_path.open("rb") as stream:
            stream.seek(self._offset)
            while True:
                line_start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    self._offset = line_start
                    break
                self._offset = stream.tell()
                try:
                    value = json.loads(line.decode("utf-8"))
                    if not isinstance(value, dict):
                        continue
                    event = AddinEvent.from_mapping(value)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                if self._remember(event.event_id):
                    try:
                        self.on_event(event)
                    except Exception:
                        # An observer must not stop the bridge reader.
                        continue
                    delivered += 1
        return delivered

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except OSError:
                pass
            self._stop.wait(self.poll_seconds)

    def _remember(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > 1024:
            removed = self._seen_order.popleft()
            self._seen.discard(removed)
        return True


def read_capability_snapshot(path: str | Path) -> dict[str, Any] | None:
    source = Path(path)
    try:
        value = json.loads(source.read_text("utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or int(value.get("schema_version") or 0) != 1:
        return None
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list):
        return None
    return value


def capability_snapshot_matches(
    snapshot: dict[str, Any] | None,
    *,
    process_id: int,
    solidworks_revision: str = "",
    connected_at_utc: str = "",
    max_clock_skew_seconds: float = 120.0,
) -> bool:
    """Return whether a durable capability file belongs to the live Add-in instance."""
    if not snapshot or process_id <= 0:
        return False
    try:
        if int(snapshot.get("process_id") or 0) != int(process_id):
            return False
    except (TypeError, ValueError):
        return False
    expected_revision = str(solidworks_revision or "").strip()
    actual_revision = str(snapshot.get("solidworks_revision") or "").strip()
    if expected_revision and actual_revision != expected_revision:
        return False
    if not connected_at_utc:
        return True
    detected = _parse_utc(str(snapshot.get("detected_at_utc") or ""))
    connected = _parse_utc(connected_at_utc)
    if detected is None or connected is None:
        return False
    return abs((detected - connected).total_seconds()) <= max(0.0, max_clock_skew_seconds)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
