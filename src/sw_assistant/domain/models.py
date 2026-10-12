from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .errors import PlanningError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionState(StrEnum):
    IDLE = "idle"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


def _json_value(value: Any, path: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PlanningError(f"{path} 必须是有限数值。")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, f"{path}.{key}") for key, item in value.items()}
    raise PlanningError(f"{path} 包含不支持的数据类型。")


@dataclass(slots=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ToolCall":
        allowed = {"tool", "name", "arguments", "description", "id"}
        unknown = set(value) - allowed
        if unknown:
            raise PlanningError(f"工具步骤包含未知字段：{', '.join(sorted(unknown))}")
        tool = str(value.get("tool") or value.get("name") or "").strip()
        if not tool:
            raise PlanningError("工具步骤缺少 tool。")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise PlanningError(f"{tool}.arguments 必须是对象。")
        return cls(
            tool=tool,
            arguments=_json_value(dict(arguments), f"{tool}.arguments"),
            description=str(value.get("description") or "").strip(),
            id=str(value.get("id") or uuid.uuid4().hex),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "description": self.description,
            "id": self.id,
        }


@dataclass(slots=True)
class Plan:
    task: str
    steps: list[ToolCall]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=utc_now)
    assumptions: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Plan":
        allowed = {"task", "steps", "id", "created_at", "assumptions"}
        unknown = set(value) - allowed
        if unknown:
            raise PlanningError(f"计划包含未知字段：{', '.join(sorted(unknown))}")
        task = str(value.get("task") or "").strip()
        if not task:
            raise PlanningError("计划缺少 task。")
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PlanningError("计划至少需要一个步骤。")
        if len(steps) > 500:
            raise PlanningError("计划步骤超过 500 步安全上限。")
        raw_assumptions = value.get("assumptions", [])
        if not isinstance(raw_assumptions, list):
            raise PlanningError("计划 assumptions 必须是数组。")
        assumptions = [str(item).strip() for item in raw_assumptions if str(item).strip()]
        if len(assumptions) > 16:
            raise PlanningError("计划假设超过 16 条安全上限。")
        return cls(
            task=task,
            steps=[ToolCall.from_mapping(step) for step in steps],
            id=str(value.get("id") or uuid.uuid4().hex),
            created_at=str(value.get("created_at") or utc_now()),
            assumptions=assumptions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "steps": [step.to_dict() for step in self.steps],
            "id": self.id,
            "created_at": self.created_at,
            "assumptions": list(self.assumptions),
        }


@dataclass(slots=True)
class ModelSnapshot:
    connected: bool = False
    document_title: str = ""
    document_path: str = ""
    document_type: str = "none"
    active_sketch: str = ""
    sketch_entity_count: int = 0
    feature_count: int = 0
    body_count: int = 0
    volume_m3: float = 0.0
    surface_area_m2: float = 0.0
    mass_kg: float = 0.0
    mass_available: bool = False
    selection_count: int = 0
    captured_at: str = field(default_factory=utc_now)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ModelSnapshot | None":
        if value is None:
            return None
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


@dataclass(slots=True)
class StepResult:
    ok: bool
    call: ToolCall
    message: str
    error_code: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    before: ModelSnapshot | None = None
    after: ModelSnapshot | None = None
    duration_ms: int = 0
    timestamp: str = field(default_factory=utc_now)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StepResult":
        return cls(
            ok=bool(value.get("ok")),
            call=ToolCall.from_mapping(value.get("call", {})),
            message=str(value.get("message") or ""),
            error_code=str(value.get("error_code") or ""),
            data=dict(value.get("data") or {}),
            before=ModelSnapshot.from_mapping(value.get("before")),
            after=ModelSnapshot.from_mapping(value.get("after")),
            duration_ms=int(value.get("duration_ms") or 0),
            timestamp=str(value.get("timestamp") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "call": self.call.to_dict(),
            "message": self.message,
            "error_code": self.error_code,
            "data": self.data,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }
