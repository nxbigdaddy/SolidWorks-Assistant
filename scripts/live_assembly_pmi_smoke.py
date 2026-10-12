from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.advanced import (
    AssemblyAssistant,
    CapabilityState,
    CapabilityStatus,
    WorkerAssemblyAdapter,
    WorkerPmiAdapter,
)
from sw_assistant.solidworks import SolidWorksWorkerClient
from sw_assistant.solidworks.gateway import SolidWorksGateway, _value


def _available(capability_id: str) -> CapabilityStatus:
    return CapabilityStatus(
        capability_id,
        CapabilityState.AVAILABLE,
        "live smoke",
        source="live-smoke",
    )


def _create_assembly(model: Path, destination: Path, template: Path) -> int:
    gateway = SolidWorksGateway(visible=True)
    title = ""
    model_title = ""
    try:
        app = gateway.connect()
        if gateway.document is not None:
            raise RuntimeError("存在活动文档，装配体实机测试拒绝启动。")
        model_document, _ = gateway._open_readonly_document(str(model))
        model_title = str(_value(model_document, "GetTitle", "") or "")
        document = app.NewDocument(str(template), 0, 0.0, 0.0)
        if document is None or int(_value(document, "GetType", 0) or 0) != 2:
            raise RuntimeError("SolidWorks 未创建临时装配体。")
        title = str(_value(document, "GetTitle", "") or "")
        assembly = gateway._cast_document(document, "AssemblyDoc")
        components = [
            assembly.AddComponent5(str(model), 0, "", False, "", 0.0, 0.0, 0.0),
            assembly.AddComponent5(str(model), 0, "", False, "", 0.03, 0.0, 0.0),
        ]
        if any(component is None for component in components):
            raise RuntimeError("SolidWorks 未插入两个测试组件。")
        document.ForceRebuild3(False)
        null_dispatch = gateway._null_dispatch()
        errors = gateway._int_reference()
        warnings = gateway._int_reference()
        saved = document.Extension.SaveAs3(
            str(destination),
            0,
            1,
            null_dispatch,
            null_dispatch,
            errors,
            warnings,
        )
        if (
            not bool(saved)
            or int(errors.value) != 0
            or not destination.is_file()
        ):
            raise RuntimeError(f"临时装配体保存失败，错误码 {errors.value!r}。")
        process_id = int(_value(app, "GetProcessID", 0) or 0)
        app.CloseDoc(str(_value(document, "GetTitle", "") or title))
        title = ""
        app.CloseDoc(model_title)
        model_title = ""
        return process_id
    finally:
        if title and gateway._sw is not None:
            try:
                gateway._sw.CloseDoc(title)
            except Exception:
                pass
        if model_title and gateway._sw is not None:
            try:
                gateway._sw.CloseDoc(model_title)
            except Exception:
                pass
        gateway.release(close_owned=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("output")
    arguments = parser.parse_args()
    model = Path(arguments.model).resolve(strict=True)
    output = Path(arguments.output).resolve(strict=False)
    if output.exists():
        raise RuntimeError("装配体实机测试目标已存在，测试不会覆盖。")
    template = Path(
        r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2025\templates\gb_assembly.asmdot"
    ).resolve(strict=True)
    process_id = _create_assembly(model, output, template)
    worker = SolidWorksWorkerClient()
    try:
        report = AssemblyAssistant(
            WorkerAssemblyAdapter(worker, _available)
        ).inspect(str(output))
        pmi = WorkerPmiAdapter(worker, _available).read_pmi(
            str(model), cancel=threading.Event()
        )
    finally:
        worker.close()
    if len(report.components) < 2:
        raise RuntimeError("装配体重开检查没有读到两个组件。")
    payload = {
        "ok": True,
        "solidworks_process_id": process_id,
        "assembly": report.to_dict(),
        "pmi_count": len(pmi),
        "pmi": [
            {
                "name": item.name,
                "kind": item.kind,
                "value": item.value,
                "unit": item.unit,
            }
            for item in pmi
        ],
    }
    result_path = output.with_name("assembly-pmi-result.json")
    if result_path.exists():
        raise RuntimeError("装配体实机测试结果文件已存在。")
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
