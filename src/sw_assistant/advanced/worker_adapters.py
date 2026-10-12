from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..domain import ToolCall, ToolError
from ..solidworks.client import ToolWorker
from .assembly import (
    AssemblyComponent,
    AssemblyReport,
    InterferenceResult,
    MateRequest,
)
from .drawing import DrawingSpec
from .manufacturing import PmiRecord
from .models import CapabilityStatus


CapabilityProvider = Callable[[str], CapabilityStatus]


class WorkerAssemblyAdapter:
    def __init__(self, worker: ToolWorker, capabilities: CapabilityProvider) -> None:
        self.worker = worker
        self.capabilities = capabilities

    def capability(self, capability_id: str) -> CapabilityStatus:
        return self.capabilities(capability_id)

    def inspect_assembly(
        self, path: str, *, cancel: threading.Event
    ) -> AssemblyReport:
        _check_cancel(cancel, "装配体检查已取消。")
        result = self.worker.call(
            ToolCall("assembly.inspect", {"path": path}, "只读检查装配体"), 300
        )
        if not result.ok:
            raise ToolError(result.message or "SolidWorks 装配体检查失败。")
        report = dict(result.data.get("report") or {})
        components = tuple(
            AssemblyComponent(
                name=str(item.get("name") or ""),
                path=str(item.get("path") or ""),
                configuration=str(item.get("configuration") or ""),
                suppressed=bool(item.get("suppressed")),
                lightweight=bool(item.get("lightweight")),
                fixed=bool(item.get("fixed")),
                mate_count=int(item.get("mate_count") or 0),
            )
            for item in report.get("components", [])
            if isinstance(item, dict)
        )
        interferences = tuple(
            InterferenceResult(
                str(item.get("component_a") or ""),
                str(item.get("component_b") or ""),
                float(item.get("volume_m3") or 0),
            )
            for item in report.get("interferences", [])
            if isinstance(item, dict)
        )
        return AssemblyReport(
            path=str(report.get("path") or path),
            components=components,
            interferences=interferences,
            missing_references=tuple(
                str(item) for item in report.get("missing_references", [])
            ),
            degrees_of_freedom=(
                int(report["degrees_of_freedom"])
                if report.get("degrees_of_freedom") is not None
                else None
            ),
            evidence=dict(report.get("evidence") or {}),
        )

    def create_mate(self, request: MateRequest, *, cancel: threading.Event) -> dict:
        _check_cancel(cancel, "配合创建已取消。")
        result = self.worker.call(
            ToolCall(
                "assembly.create_mate",
                {
                    "assembly_path": request.assembly_path,
                    "first_reference": request.first_reference,
                    "second_reference": request.second_reference,
                    "mate_type": request.mate_type,
                    "alignment": request.alignment,
                    "distance_mm": request.distance_mm,
                    "angle_degrees": request.angle_degrees,
                },
                "使用持久引用创建配合并保留为未保存修改",
            ),
            300,
        )
        if not result.ok:
            raise ToolError(result.message or "SolidWorks 创建配合失败。")
        return dict(result.data)


class WorkerDrawingAdapter:
    def __init__(self, worker: ToolWorker, capabilities: CapabilityProvider) -> None:
        self.worker = worker
        self.capabilities = capabilities

    def capability(self, capability_id: str) -> CapabilityStatus:
        return self.capabilities(capability_id)

    def generate_drawing(self, spec: DrawingSpec, *, cancel: threading.Event) -> dict:
        _check_cancel(cancel, "工程图生成已取消。")
        result = self.worker.call(
            ToolCall(
                "drawing.generate",
                {
                    "model_path": spec.model_path,
                    "destination": spec.destination,
                    "template_path": spec.template_path,
                    "configuration": spec.configuration,
                    "views": [
                        {
                            "name": item.name,
                            "orientation": item.orientation,
                            "scale": item.scale,
                            "projected_from": item.projected_from,
                            "x": item.x,
                            "y": item.y,
                        }
                        for item in spec.views
                    ],
                    "include_bom": spec.include_bom,
                    "include_balloons": spec.include_balloons,
                    "import_model_dimensions": spec.import_model_dimensions,
                    "properties": dict(spec.properties),
                },
                "生成工程图副本并重开验证",
            ),
            600,
        )
        if not result.ok:
            traceback_text = str(result.data.get("traceback") or "").strip()
            detail = result.message or "SolidWorks 工程图生成失败。"
            if traceback_text:
                detail += "\n" + traceback_text
            raise ToolError(detail)
        return dict(result.data)


class WorkerPmiAdapter:
    def __init__(self, worker: ToolWorker, capabilities: CapabilityProvider) -> None:
        self.worker = worker
        self.capabilities = capabilities

    def capability(self, capability_id: str) -> CapabilityStatus:
        return self.capabilities(capability_id)

    def read_pmi(self, path: str, *, cancel: threading.Event) -> tuple[PmiRecord, ...]:
        _check_cancel(cancel, "PMI 读取已取消。")
        result = self.worker.call(
            ToolCall("pmi.read", {"path": str(Path(path))}, "只读读取 DimXpert PMI"),
            300,
        )
        if not result.ok:
            raise ToolError(result.message or "SolidWorks PMI 读取失败。")
        return tuple(
            PmiRecord(
                name=str(item.get("name") or ""),
                kind=str(item.get("kind") or "unknown"),
                value=_optional_float(item.get("value")),
                unit=str(item.get("unit") or ""),
                tolerance_upper=_optional_float(item.get("tolerance_upper")),
                tolerance_lower=_optional_float(item.get("tolerance_lower")),
                datum_references=tuple(
                    str(value) for value in item.get("datum_references", [])
                ),
            )
            for item in result.data.get("pmi", [])
            if isinstance(item, dict)
        )


def _check_cancel(cancel: threading.Event, message: str) -> None:
    if cancel.is_set():
        raise InterruptedError(message)


def _optional_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
