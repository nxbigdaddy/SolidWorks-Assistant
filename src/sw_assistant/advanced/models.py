from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from ..domain import utc_now


class CapabilityState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    id: str
    state: CapabilityState
    reason: str
    source: str = "runtime"
    version: str = ""
    checked_at: str = field(default_factory=utc_now)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.state is CapabilityState.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True, slots=True)
class SelectionItem:
    index: int
    selection_type: int
    type_name: str = ""
    name: str = ""
    component_path: str = ""
    persistent_ref: str = ""


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    source_session_id: str
    sequence: int
    captured_at: str
    process_id: int = 0
    solidworks_revision: str = ""
    command_in_progress: bool = False
    document: Mapping[str, Any] = field(default_factory=dict)
    selections: tuple[SelectionItem, ...] = ()
    capabilities: tuple[CapabilityStatus, ...] = ()
    stale_after_ms: int = 15_000

    def is_stale(self, *, now: datetime | None = None) -> bool:
        try:
            captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return (current - captured).total_seconds() * 1000 > self.stale_after_ms

    def usable_for_write(self) -> tuple[bool, str]:
        if self.is_stale():
            return False, "SolidWorks 上下文已经过期，请重新采集。"
        if self.command_in_progress:
            return False, "SolidWorks 正在执行其他命令。"
        if not self.document:
            return False, "当前没有活动文档。"
        if bool(self.document.get("read_only")):
            return False, "当前文档为只读。"
        return True, "上下文可用于写操作。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_session_id": self.source_session_id,
            "sequence": self.sequence,
            "captured_at": self.captured_at,
            "process_id": self.process_id,
            "solidworks_revision": self.solidworks_revision,
            "command_in_progress": self.command_in_progress,
            "document": dict(self.document),
            "selections": [asdict(item) for item in self.selections],
            "capabilities": [item.to_dict() for item in self.capabilities],
            "stale_after_ms": self.stale_after_ms,
        }


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class DiagnosticIssue:
    rule_id: str
    severity: Severity
    message: str
    scope: str = "document"
    object_name: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    recommendation: str = ""
    auto_fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["severity"] = self.severity.value
        return value


@dataclass(frozen=True, slots=True)
class HealthReport:
    document_path: str
    status: str
    score: int
    issues: tuple[DiagnosticIssue, ...]
    checked_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_path": self.document_path,
            "status": self.status,
            "score": self.score,
            "issues": [item.to_dict() for item in self.issues],
            "checked_at": self.checked_at,
        }


def capability_from_mapping(value: Mapping[str, Any]) -> CapabilityStatus:
    raw_state = value.get("state")
    if raw_state is None and "available" in value:
        raw_state = "available" if bool(value.get("available")) else "unavailable"
    try:
        state = CapabilityState(str(raw_state or "unknown"))
    except ValueError:
        state = CapabilityState.UNKNOWN
    raw_evidence = value.get("evidence")
    evidence = dict(raw_evidence) if isinstance(raw_evidence, Mapping) else {}
    return CapabilityStatus(
        id=str(value.get("id") or ""),
        state=state,
        reason=str(value.get("reason") or value.get("evidence") or ""),
        source=str(value.get("source") or "runtime"),
        version=str(value.get("version") or ""),
        checked_at=str(value.get("checked_at") or utc_now()),
        evidence=evidence,
    )
