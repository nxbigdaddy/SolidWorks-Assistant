from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import CapabilityState, CapabilityStatus, DiagnosticIssue, Severity


@dataclass(frozen=True, slots=True)
class PmiRecord:
    name: str
    kind: str
    value: float | None = None
    unit: str = ""
    tolerance_upper: float | None = None
    tolerance_lower: float | None = None
    datum_references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManufacturingReport:
    path: str
    pmi: tuple[PmiRecord, ...]
    issues: tuple[DiagnosticIssue, ...]
    pmi_source: str
    capability: CapabilityStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "pmi": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "value": item.value,
                    "unit": item.unit,
                    "tolerance_upper": item.tolerance_upper,
                    "tolerance_lower": item.tolerance_lower,
                    "datum_references": list(item.datum_references),
                }
                for item in self.pmi
            ],
            "issues": [item.to_dict() for item in self.issues],
            "pmi_source": self.pmi_source,
            "capability": self.capability.to_dict(),
        }


class PmiAdapter(Protocol):
    def capability(self, capability_id: str) -> CapabilityStatus: ...

    def read_pmi(self, path: str, *, cancel: threading.Event) -> tuple[PmiRecord, ...]: ...


class ManufacturingAnalyzer:
    def __init__(self, adapter: PmiAdapter | None = None) -> None:
        self.adapter = adapter

    def analyze(
        self,
        path: str,
        manifest: Mapping[str, Any],
        *,
        metrics: Mapping[str, float] | None = None,
        cancel: threading.Event | None = None,
    ) -> ManufacturingReport:
        source = Path(path).resolve(strict=False)
        if source.suffix.casefold() not in {".sldprt", ".sldasm"}:
            raise ValueError("制造分析只接受零件或装配体。")
        cancellation = cancel or threading.Event()
        if cancellation.is_set():
            raise InterruptedError("制造分析已取消。")
        if self.adapter is None:
            capability = CapabilityStatus(
                "pmi.read",
                state=CapabilityState.UNAVAILABLE,
                reason="未配置 PMI/DimXpert 适配器；仅执行本地 DFM 规则。",
                source="configuration",
            )
            pmi: tuple[PmiRecord, ...] = ()
            pmi_source = "none"
        else:
            capability = self.adapter.capability("pmi.read")
            if capability.available:
                pmi = self.adapter.read_pmi(str(source), cancel=cancellation)
                pmi_source = capability.source
            else:
                pmi = ()
                pmi_source = "unavailable"
        issues = self._dfm_rules(manifest, pmi, metrics or {})
        return ManufacturingReport(str(source), pmi, tuple(issues), pmi_source, capability)

    @staticmethod
    def _dfm_rules(
        manifest: Mapping[str, Any],
        pmi: tuple[PmiRecord, ...],
        metrics: Mapping[str, float],
    ) -> list[DiagnosticIssue]:
        issues: list[DiagnosticIssue] = []
        minimum_wall = float(metrics.get("minimum_wall_mm", 0) or 0)
        wall_limit = float(metrics.get("wall_limit_mm", 0.8) or 0.8)
        if minimum_wall and minimum_wall < wall_limit:
            issues.append(
                DiagnosticIssue(
                    "dfm.thin_wall",
                    Severity.WARNING,
                    f"最小壁厚 {minimum_wall:g} mm 低于规则值 {wall_limit:g} mm。",
                    evidence={"minimum_wall_mm": minimum_wall, "limit_mm": wall_limit},
                    recommendation="根据材料和工艺确认壁厚，必要时加厚或增加筋。",
                )
            )
        hole_depth = float(metrics.get("maximum_hole_depth_mm", 0) or 0)
        hole_diameter = float(metrics.get("minimum_hole_diameter_mm", 0) or 0)
        if hole_depth and hole_diameter and hole_depth / hole_diameter > 8:
            issues.append(
                DiagnosticIssue(
                    "dfm.deep_hole_ratio",
                    Severity.WARNING,
                    f"深孔深径比为 {hole_depth / hole_diameter:.1f}，超过规则值 8。",
                    evidence={"depth_mm": hole_depth, "diameter_mm": hole_diameter},
                    recommendation="确认刀具可达性、排屑和分步加工方案。",
                )
            )
        for record in pmi:
            if record.kind in {"size", "diameter", "distance"} and (
                record.tolerance_upper is None or record.tolerance_lower is None
            ):
                issues.append(
                    DiagnosticIssue(
                        "pmi.tolerance_missing",
                        Severity.INFO,
                        f"PMI 尺寸 {record.name} 没有完整公差。",
                        scope="pmi",
                        object_name=record.name,
                        recommendation="确认该尺寸是否使用未在模型中表达的通用公差。",
                    )
                )
        if not manifest.get("material"):
            issues.append(
                DiagnosticIssue(
                    "dfm.material_missing",
                    Severity.WARNING,
                    "未指定材料，无法可靠判断加工或成形规则。",
                    recommendation="指定材料牌号后重新运行 DFM。",
                )
            )
        return issues
