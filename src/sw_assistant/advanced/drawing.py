from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import CapabilityStatus


@dataclass(frozen=True, slots=True)
class DrawingViewSpec:
    name: str
    orientation: str
    scale: float = 1.0
    projected_from: str = ""
    x: float = 0.0
    y: float = 0.0

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("工程图视图名称不能为空。")
        if self.orientation not in {"front", "top", "right", "isometric", "projected", "section"}:
            raise ValueError(f"不支持的工程图视图方向：{self.orientation}")
        if not 0.001 <= float(self.scale) <= 1000:
            raise ValueError("工程图视图比例超出安全范围。")
        if self.orientation in {"projected", "section"} and not self.projected_from:
            raise ValueError(f"{self.orientation} 视图必须指定父视图。")


@dataclass(frozen=True, slots=True)
class DrawingSpec:
    model_path: str
    destination: str
    template_path: str
    views: tuple[DrawingViewSpec, ...]
    configuration: str = ""
    include_bom: bool = False
    include_balloons: bool = False
    import_model_dimensions: bool = False
    properties: Mapping[str, str] = field(default_factory=dict)

    def validate(self, *, require_files: bool = True) -> None:
        model = Path(self.model_path).resolve(strict=False)
        destination = Path(self.destination).resolve(strict=False)
        template = Path(self.template_path).resolve(strict=False)
        if model.suffix.casefold() not in {".sldprt", ".sldasm"}:
            raise ValueError("工程图源模型必须是 SLDPRT 或 SLDASM。")
        if destination.suffix.casefold() != ".slddrw":
            raise ValueError("工程图输出必须是 SLDDRW。")
        if template.suffix.casefold() not in {".drwdot", ".slddrw"}:
            raise ValueError("工程图模板必须是 DRWDOT 或 SLDDRW。")
        if require_files and (not model.is_file() or not template.is_file()):
            raise ValueError("工程图源模型或模板不存在。")
        if destination == model or destination == template:
            raise ValueError("工程图输出不能覆盖源模型或模板。")
        if not 1 <= len(self.views) <= 20:
            raise ValueError("工程图必须包含 1 到 20 个视图。")
        names: set[str] = set()
        for view in self.views:
            view.validate()
            folded = view.name.casefold()
            if folded in names:
                raise ValueError(f"工程图视图名称重复：{view.name}")
            names.add(folded)
        for view in self.views:
            if view.projected_from and view.projected_from.casefold() not in names:
                raise ValueError(f"视图 {view.name} 的父视图不存在。")
        if self.include_balloons and not self.include_bom:
            raise ValueError("自动气泡必须与 BOM 一起启用。")
        if self.include_bom and model.suffix.casefold() != ".sldasm":
            raise ValueError("BOM 仅适用于装配体工程图。")


class DrawingAdapter(Protocol):
    def capability(self, capability_id: str) -> CapabilityStatus: ...

    def generate_drawing(
        self, spec: DrawingSpec, *, cancel: threading.Event
    ) -> Mapping[str, Any]: ...


class DrawingAutomationService:
    def __init__(self, adapter: DrawingAdapter) -> None:
        self.adapter = adapter

    def generate(
        self, spec: DrawingSpec, *, cancel: threading.Event | None = None
    ) -> Mapping[str, Any]:
        spec.validate()
        capability = self.adapter.capability("drawing.generate")
        if not capability.available:
            raise RuntimeError(f"工程图自动生成不可用：{capability.reason}")
        cancellation = cancel or threading.Event()
        if cancellation.is_set():
            raise InterruptedError("工程图生成已取消。")
        result = self.adapter.generate_drawing(spec, cancel=cancellation)
        observed = {str(item).casefold() for item in result.get("views", [])}
        expected = {item.name.casefold() for item in spec.views}
        if not expected.issubset(observed):
            raise RuntimeError(f"工程图视图验证失败，缺少：{sorted(expected - observed)}")
        if spec.include_bom and not bool(result.get("bom_created")):
            raise RuntimeError("工程图要求 BOM，但后验验证没有找到 BOM。")
        if Path(str(result.get("destination") or "")).resolve(strict=False) != Path(
            spec.destination
        ).resolve(strict=False):
            raise RuntimeError("工程图适配器返回了错误的输出路径。")
        return result
