from __future__ import annotations

import threading
from typing import Any, Mapping

from ..advanced import (
    CapabilityState,
    CapabilityStatus,
    ContextSnapshot,
    SelectionItem,
)
from .event_bridge import AddinEvent


class SolidWorksContextStore:
    """Keeps the latest immutable Add-in context and rejects old event sequences."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: ContextSnapshot | None = None
        self._sequences: dict[str, int] = {}
        self._capabilities: tuple[CapabilityStatus, ...] = ()

    def set_capabilities(self, snapshot: Mapping[str, Any] | None) -> None:
        values = snapshot.get("capabilities") if snapshot else None
        version = str(snapshot.get("solidworks_revision") or "") if snapshot else ""
        capabilities: list[CapabilityStatus] = []
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, Mapping) or not value.get("id"):
                    continue
                available = bool(value.get("available"))
                capabilities.append(
                    CapabilityStatus(
                        str(value["id"]),
                        CapabilityState.AVAILABLE if available else CapabilityState.UNAVAILABLE,
                        str(value.get("evidence") or "Interop reflection"),
                        source="solidworks-addin",
                        version=version,
                        evidence={
                            "type_name": value.get("type"),
                            "member_name": value.get("member"),
                            "member_kind": value.get("member_kind"),
                            "evidence_level": value.get("evidence_level", "declaration"),
                        },
                    )
                )
        with self._lock:
            self._capabilities = tuple(capabilities)

    def update(self, event: AddinEvent) -> ContextSnapshot | None:
        raw = event.payload.get("context")
        if not isinstance(raw, Mapping):
            return None
        with self._lock:
            previous_sequence = self._sequences.get(event.session_id, -1)
            if event.sequence <= previous_sequence:
                return None
            self._sequences[event.session_id] = event.sequence
            selections: list[SelectionItem] = []
            raw_selections = raw.get("selections")
            if isinstance(raw_selections, list):
                for value in raw_selections:
                    if not isinstance(value, Mapping) or "read_error" in value:
                        continue
                    selections.append(
                        SelectionItem(
                            index=self._integer(value.get("index")),
                            selection_type=self._integer(value.get("selection_type")),
                            type_name=str(value.get("type_name") or ""),
                            name=str(value.get("name") or ""),
                            component_path=str(value.get("component_path") or ""),
                            persistent_ref=str(value.get("persistent_ref") or ""),
                        )
                    )
            document = raw.get("document")
            if not isinstance(document, Mapping):
                document = {}
            stale_after = max(
                1000,
                min(self._integer(raw.get("stale_after_ms"), 15000), 60000),
            )
            snapshot = ContextSnapshot(
                source_session_id=event.session_id,
                sequence=event.sequence,
                captured_at=str(raw.get("captured_at") or event.timestamp_utc),
                process_id=self._integer(raw.get("process_id"), event.process_id),
                solidworks_revision=str(raw.get("solidworks_revision") or ""),
                command_in_progress=bool(raw.get("command_in_progress")),
                document=dict(document),
                selections=tuple(selections),
                capabilities=self._capabilities,
                stale_after_ms=stale_after,
            )
            self._latest = snapshot
            return snapshot

    def latest(self, *, require_fresh: bool = False) -> ContextSnapshot | None:
        with self._lock:
            value = self._latest
        if value is not None and require_fresh and value.is_stale():
            return None
        return value

    def clear(self) -> None:
        with self._lock:
            self._latest = None

    @staticmethod
    def _integer(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)
