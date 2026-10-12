from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import CapabilityStatus


@dataclass(frozen=True, slots=True)
class AssemblyComponent:
    name: str
    path: str
    configuration: str = ""
    suppressed: bool = False
    lightweight: bool = False
    fixed: bool = False
    mate_count: int = 0


@dataclass(frozen=True, slots=True)
class InterferenceResult:
    component_a: str
    component_b: str
    volume_m3: float


@dataclass(frozen=True, slots=True)
class AssemblyReport:
    path: str
    components: tuple[AssemblyComponent, ...]
    interferences: tuple[InterferenceResult, ...] = ()
    missing_references: tuple[str, ...] = ()
    degrees_of_freedom: int | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "components": [asdict(item) for item in self.components],
            "interferences": [asdict(item) for item in self.interferences],
            "missing_references": list(self.missing_references),
            "degrees_of_freedom": self.degrees_of_freedom,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class MateRequest:
    assembly_path: str
    first_reference: str
    second_reference: str
    mate_type: str
    alignment: str = "closest"
    distance_mm: float = 0.0
    angle_degrees: float = 0.0

    def validate(self) -> None:
        if Path(self.assembly_path).suffix.casefold() != ".sldasm":
            raise ValueError("配合目标必须是 SLDASM。")
        if not self.first_reference or not self.second_reference:
            raise ValueError("配合两端都必须使用已保存的持久引用。")
        if self.first_reference == self.second_reference:
            raise ValueError("配合的两个引用不能相同。")
        if self.mate_type not in {"coincident", "concentric", "distance", "angle", "parallel", "perpendicular"}:
            raise ValueError(f"不支持的配合类型：{self.mate_type}")
        if abs(float(self.distance_mm)) > 1_000_000 or abs(float(self.angle_degrees)) > 360:
            raise ValueError("配合距离或角度超出安全范围。")


class AssemblyAdapter(Protocol):
    def capability(self, capability_id: str) -> CapabilityStatus: ...

    def inspect_assembly(
        self, path: str, *, cancel: threading.Event
    ) -> AssemblyReport: ...

    def create_mate(self, request: MateRequest, *, cancel: threading.Event) -> Mapping[str, Any]: ...


class AssemblyAssistant:
    def __init__(self, adapter: AssemblyAdapter) -> None:
        self.adapter = adapter

    def inspect(self, path: str, *, cancel: threading.Event | None = None) -> AssemblyReport:
        source = Path(path).resolve(strict=False)
        if source.suffix.casefold() != ".sldasm" or not source.is_file():
            raise ValueError("装配体检查需要现有 SLDASM 文件。")
        self._require("assembly.inspect")
        cancellation = cancel or threading.Event()
        if cancellation.is_set():
            raise InterruptedError("装配体检查已取消。")
        report = self.adapter.inspect_assembly(str(source), cancel=cancellation)
        if Path(report.path).resolve(strict=False) != source:
            raise RuntimeError("装配体适配器返回了错误的文档。")
        return report

    def create_mate(
        self, request: MateRequest, *, cancel: threading.Event | None = None
    ) -> Mapping[str, Any]:
        request.validate()
        self._require("assembly.create_mate")
        cancellation = cancel or threading.Event()
        if cancellation.is_set():
            raise InterruptedError("配合创建已取消。")
        result = self.adapter.create_mate(request, cancel=cancellation)
        if not bool(result.get("created")) or int(result.get("error_code") or 0) != 0:
            raise RuntimeError(f"SolidWorks 未确认配合创建成功：{dict(result)}")
        return result

    def _require(self, capability_id: str) -> None:
        status = self.adapter.capability(capability_id)
        if not status.available:
            raise RuntimeError(f"能力 {capability_id} 不可用：{status.reason}")
