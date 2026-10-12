from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.advanced import (
    DrawingAutomationService,
    DrawingSpec,
    DrawingViewSpec,
    FamilyVariant,
    ProductFamilyService,
    ProductFamilySpec,
    WorkerDrawingAdapter,
    WorkerProductFamilyAdapter,
)
from sw_assistant.domain import ToolCall
from sw_assistant.solidworks import SolidWorksWorkerClient
from sw_assistant.solidworks.gateway import SolidWorksGateway, _items, _value


def _available(capability_id):
    from sw_assistant.advanced import CapabilityState, CapabilityStatus

    return CapabilityStatus(
        capability_id,
        CapabilityState.AVAILABLE,
        "live smoke uses locally reflected SolidWorks 2025 API",
        source="live-smoke",
    )


def _drawing_template() -> str:
    patterns = (
        r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\templates\*.drwdot",
        r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\lang\*\Tutorial\*.drwdot",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(path for path in glob.glob(pattern) if Path(path).is_file())
    return sorted(values, key=str.casefold, reverse=True)[0] if values else ""


def _create_source_part(path: Path) -> tuple[int, str]:
    gateway = SolidWorksGateway(visible=True)
    title = ""
    try:
        app = gateway.connect()
        if gateway.document is not None:
            raise RuntimeError("存在活动文档；为避免影响用户工作，实机高级测试已拒绝启动。")
        for call in (
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "front"}),
            ToolCall("sketch.circle", {"x_mm": 0, "y_mm": 0, "radius_mm": 5}),
            ToolCall(
                "feature.extrude",
                {
                    "depth_mm": 10,
                    "operation": "boss",
                    "end_condition": "blind",
                    "reverse": False,
                    "merge": True,
                },
            ),
        ):
            gateway.execute(call)
        document = gateway.require_document(part=True)
        title = str(_value(document, "GetTitle", "") or "")
        null_dispatch = gateway._null_dispatch()
        save_errors = gateway._int_reference()
        save_warnings = gateway._int_reference()
        result = document.Extension.SaveAs3(
            str(path),
            0,
            1,
            null_dispatch,
            null_dispatch,
            save_errors,
            save_warnings,
        )
        success = next(
            (value for value in _items(result) if isinstance(value, bool)), bool(result)
        )
        if not success or int(save_errors.value) != 0 or not path.is_file():
            raise RuntimeError(f"临时零件保存失败，错误码 {int(save_errors.value)}。")
        process_id = int(app.GetProcessID())
        app.CloseDoc(str(_value(document, "GetTitle", "") or title))
        title = ""
        return process_id, str(_value(app, "RevisionNumber", "") or "")
    finally:
        if title and gateway._sw is not None:
            try:
                gateway._sw.CloseDoc(title)
            except Exception:
                pass
        gateway.release(close_owned=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise RuntimeError("实机验证输出目录已存在；测试不会删除或覆盖现有文件。")
    output.mkdir(parents=True)
    source = output / "advanced-source.SLDPRT"
    family = output / "advanced-family.SLDPRT"
    drawing = output / "advanced-family.SLDDRW"
    process_id, revision = _create_source_part(source)
    gateway = SolidWorksGateway(visible=True)
    try:
        gateway.connect()
        manifest = gateway._inspect_document(str(source))
    finally:
        gateway.release(close_owned=True)
    dimensions = [
        item
        for item in manifest.get("dimensions", [])
        if item.get("name") and float(item.get("display_value_mm") or 0) > 0
    ]
    if not dimensions:
        raise RuntimeError("临时零件没有可用于产品族的尺寸。")
    dimension = str(dimensions[0]["name"])
    base_value = float(dimensions[0]["display_value_mm"])
    worker = SolidWorksWorkerClient()
    try:
        family_result = ProductFamilyService(
            WorkerProductFamilyAdapter(worker)
        ).generate(
            ProductFamilySpec(
                str(source),
                str(family),
                (
                    FamilyVariant("规格-A", {dimension: base_value * 0.8}, {"规格": "A"}),
                    FamilyVariant("规格-B", {dimension: base_value * 1.2}, {"规格": "B"}),
                ),
            )
        )
        template = _drawing_template()
        if not template:
            raise RuntimeError("本机没有找到 DRWDOT 工程图模板。")
        drawing_result = DrawingAutomationService(
            WorkerDrawingAdapter(worker, _available)
        ).generate(
            DrawingSpec(
                str(family),
                str(drawing),
                template,
                (
                    DrawingViewSpec("主视图", "front", x=0.08, y=0.18),
                    DrawingViewSpec("等轴测", "isometric", x=0.20, y=0.18),
                ),
                import_model_dimensions=True,
            )
        )
    finally:
        worker.close()
    payload = {
        "ok": True,
        "solidworks_process_id": process_id,
        "solidworks_revision": revision,
        "source": str(source),
        "source_dimension": dimension,
        "source_dimension_mm": base_value,
        "family": {
            "path": str(family),
            "configurations": family_result["configurations"],
            "verified_variants": family_result["variants"],
        },
        "drawing": {
            "path": str(drawing),
            "views": drawing_result["views"],
            "bom_created": drawing_result["bom_created"],
        },
    }
    report = output / "result.json"
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
