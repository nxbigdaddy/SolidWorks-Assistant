"""Opt-in real worker/CAD smoke; a new unsaved fixture, no models or existing CAD files."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sw_assistant.domain import ToolCall
from sw_assistant.solidworks import SolidWorksWorkerClient
from sw_assistant.tools import build_tool_registry


EXPECTED_VOLUME_MM3 = 64 * 32 * 4 + math.pi * (8**2 + 7.5**2 + 8**2) * 4


def preparation_plan() -> dict[str, Any]:
    return {
        "status": "prepared_not_executed", "external_model_requests": 0,
        "fixture": "new unsaved part; 64x32x4 mm base; three r8 circles at x=-16,0,16 mm",
        "sequence": ["create and verify base", "create three tangent circles and read stable references",
                     "verify profile status 12 and failed extrusion leaves the sketch unchanged",
                     "set only the middle circle radius from 8 to 7.5 mm",
                     "verify isolated geometry change, then extrude to 8 mm from front plane",
                     "verify one solid, no active sketch, no feature errors, theoretical volume"],
        "expected_final_volume_mm3": EXPECTED_VOLUME_MM3,
        "safety": ["requires --execute --exclusive-confirmed and an idle GUI",
                   "existing SolidWorks is allowed only when the active unsaved fixture matches a prior local smoke report",
                   "opens no CAD file and saves no CAD document",
                   "leaves the unsaved fixture and SolidWorks open, never calls QuitApp/ExitApp",
                   "unexpected failure stops further writes and preserves local evidence"],
    }


def require_safe_solidworks_state(fixture_report: str | None = None) -> None:
    if os.name != "nt":
        raise RuntimeError("The real SolidWorks smoke is Windows-only.")
    result = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", timeout=15, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if not any(row and row[0].casefold() == "sldworks.exe" for row in csv.reader(result.stdout.splitlines())):
        return
    if not fixture_report:
        raise RuntimeError("Existing SLDWORKS requires --existing-fixture-report; do not attach to user CAD.")
    expected = load_fixture_checkpoint(Path(fixture_report))
    import pythoncom
    from win32com.client import GetActiveObject
    from sw_assistant.solidworks.gateway import SolidWorksGateway, _read_member

    pythoncom.CoInitialize()
    gateway = SolidWorksGateway()
    app = document = None
    try:
        app = GetActiveObject("SldWorks.Application")
        document = _read_member(app, "ActiveDoc")
        if (document is None or _read_member(document, "GetPathName")
                or _read_member(document, "GetTitle") != expected["document_title"]):
            raise RuntimeError("Active document is not the known unsaved smoke fixture; refusing to proceed.")
        gateway._sw = app
        current = gateway.snapshot().to_dict()
        if not matches_fixture_checkpoint(expected, current):
            raise RuntimeError("Active fixture changed since its smoke report; refusing to proceed.")
    finally:
        gateway.release()
        document = app = None
        pythoncom.CoUninitialize()


def load_fixture_checkpoint(path: Path) -> dict[str, Any]:
    path = path.resolve(strict=True)
    report = json.loads(path.read_text(encoding="utf-8"))
    circle_root = (ROOT / "artifacts" / "v511-live-circle-repair").resolve()
    agent_root = (ROOT / "artifacts" / "configured-agent-smoke").resolve()
    if path.is_relative_to(circle_root) and path.name == "result.json":
        if report.get("plan", {}).get("fixture") != preparation_plan()["fixture"]:
            raise RuntimeError("Not a circle-repair fixture report.")
        steps = report.get("steps", [])
        if not any(item.get("ok") and item.get("call", {}).get("tool") == "document.new_part" for item in steps):
            raise RuntimeError("Report does not prove creation of a new fixture.")
        expected = next((item["after"] for item in reversed(steps) if isinstance(item.get("after"), dict)), None)
    elif path.is_relative_to(agent_root) and path.parent.name == "tasks" and path.suffix == ".json":
        summary = json.loads((path.parent.parent / "result.json").read_text(encoding="utf-8"))
        if (summary.get("real_cad") is not True or summary.get("synthetic_data_only") is not True
                or summary.get("cad_files_saved") != 0 or summary.get("scenario") != "cube"
                or summary.get("status") != "stopped" or summary.get("completed_steps") != 0):
            raise RuntimeError("Agent report is not the authorized stopped synthetic blank cube fixture.")
        observation = report.get("latest_observation", {})
        expected = observation.get("snapshot")
        if (observation.get("complete") is not True or not observation.get("document_identity")
                or not isinstance(expected, dict) or expected.get("document_type") != "part"
                or expected.get("body_count") != 0 or expected.get("sketch_entity_count") != 0
                or expected.get("active_sketch") != "" or expected.get("volume_m3") != 0.0):
            raise RuntimeError("Agent task lacks a complete known blank fixture checkpoint.")
    else:
        raise RuntimeError("Existing fixture report must be within the local isolated smoke artifacts.")
    if not expected or expected.get("document_path") or not expected.get("document_title"):
        raise RuntimeError("Report lacks an identifiable unsaved fixture checkpoint.")
    return expected


def matches_fixture_checkpoint(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    fields = ("document_title", "document_path", "document_type", "active_sketch", "sketch_entity_count",
              "body_count", "feature_count", "volume_m3")
    return not current.get("document_path") and same_state(
        {key: expected.get(key) for key in fields}, {key: current.get(key) for key in fields})


def same_state(left: Any, right: Any, field: str = "") -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_state(value, right[key], key) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_state(a, b, field) for a, b in zip(left, right, strict=True))
    if field == "volume_m3":
        return math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-12)
    return left == right


def geometry_state(observation: dict[str, Any]) -> dict[str, Any]:
    snapshot = observation["snapshot"]
    return {"document_identity": observation["document_identity"],
            "features": observation["features"], "dimensions": observation["dimensions"],
            "geometry": observation["active_sketch_geometry"],
            "snapshot": {key: snapshot[key] for key in (
                "document_title", "document_path", "document_type", "active_sketch", "sketch_entity_count",
                "body_count", "feature_count", "volume_m3")}}


def run_fixture(worker: Any, report: dict[str, Any], emit=print) -> None:
    registry = build_tool_registry()
    bound_identity = None
    observations: dict[str, Any] = report.setdefault("observations", {})

    def call(tool: str, arguments: dict[str, Any] | None = None, *, expected_failure: bool = False):
        normalized = registry.validate(ToolCall(tool, arguments or {}))
        report["phase"] = tool
        emit(f"CALL {tool}", flush=True)
        result = worker.call(normalized, 180)
        report.setdefault("steps", []).append(result.to_dict())
        emit(f"RESULT {tool}: ok={result.ok} {result.message}", flush=True)
        if expected_failure:
            if result.ok:
                raise RuntimeError("Tangent-circle extrusion unexpectedly succeeded; fixture stopped without repair.")
            return result
        if not result.ok:
            raise RuntimeError(f"{tool} failed: {result.message}")
        check = registry.postcondition(tool)
        if check is not None:
            valid, reason = check(normalized, result.data, result.before, result.after)
            if not valid:
                raise RuntimeError(f"{tool} postcondition failed: {reason}")
        return result

    def observe(label: str) -> dict[str, Any]:
        data = call("model.observe").data
        observations[label] = data
        if not data.get("complete") or not data.get("document_identity"):
            raise RuntimeError(f"{label}: document observation is incomplete: {data.get('issues')}")
        if bound_identity is not None and data["document_identity"] != bound_identity:
            raise RuntimeError(f"{label}: active document identity changed; refusing further writes.")
        if data["snapshot"].get("document_path"):
            raise RuntimeError(f"{label}: fixture is no longer unsaved; refusing further writes.")
        return data

    call("session.ensure")
    call("document.new_part")
    initial = observe("new_document")
    bound_identity = initial["document_identity"]
    call("sketch.begin", {"plane": "front"})
    call("sketch.rectangle", {"width_mm": 64, "height_mm": 32})
    call("feature.extrude", {"depth_mm": 4})
    observe("base")
    call("sketch.begin", {"plane": "front"})
    creations = [call("sketch.circle", {"x_mm": x, "y_mm": 0, "radius_mm": 8}).data["applied"]
                 for x in (-16, 0, 16)]
    if any(item.get("creation_verified") is not True for item in creations):
        raise RuntimeError("Real circle creation lacks verified ownership/geometry; repair is not authorized.")
    tangent = observe("tangent_circles")
    inspection = call("sketch.inspect").data
    report["tangent_inspection"] = inspection
    sketch = inspection.get("sketch", {})
    if (not inspection.get("complete") or sketch.get("geometry_complete") is not True
            or tangent.get("active_sketch_geometry_complete") is not True):
        raise RuntimeError("Tangent sketch identity/geometry investigation is incomplete.")
    boss_checks = [item for item in sketch.get("profile_checks", []) if item.get("usage") == "boss-extrude"]
    if len(boss_checks) != 1 or boss_checks[0].get("status") != 12:
        raise RuntimeError("Expected tangent profile status 12 was not reproduced; preserve fixture for inspection.")
    call("feature.extrude", {"depth_mm": 8}, expected_failure=True)
    unchanged = observe("after_expected_failure")
    if not same_state(geometry_state(tangent), geometry_state(unchanged)):
        raise RuntimeError("Failed extrusion changed the model; no repair will be attempted.")
    reference = creations[1]["entity_ref"]
    matches = [item for item in sketch.get("segments", []) if item.get("entity_ref") == reference]
    if len(matches) != 1 or matches[0].get("editable") is not True or matches[0].get("radius_mm") != 8:
        raise RuntimeError("Middle circle ownership or expected old radius is not unambiguous.")
    repair = call("sketch.set_circle_radius", {"entity_ref": reference, "expected_radius_mm": 8, "radius_mm": 7.5})
    report["repair_evidence"] = repair.data
    repaired = observe("after_repair")
    if repaired.get("active_sketch_geometry_complete") is not True:
        raise RuntimeError("Repaired sketch could not be independently read back.")
    call("feature.extrude", {"depth_mm": 8})
    final = observe("final")
    snapshot = final["snapshot"]
    volume = snapshot["volume_m3"] * 1e9
    checks = {"one_solid": snapshot["body_count"] == 1,
              "no_active_sketch": not snapshot["active_sketch"],
              "no_feature_errors": all(item.get("error_code") == 0 for item in final["features"]),
              "volume": math.isclose(volume, EXPECTED_VOLUME_MM3, rel_tol=1e-7, abs_tol=0.001),
              "unsaved": snapshot["document_path"] == ""}
    report["acceptance"] = {"checks": checks, "actual_volume_mm3": volume,
                             "expected_volume_mm3": EXPECTED_VOLUME_MM3}
    if not all(checks.values()):
        raise RuntimeError("Final fixture acceptance failed.")
    report.update(status="passed", phase="completed", message="Tangent failure, bounded radius repair and final solid verified.")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--exclusive-confirmed", action="store_true", help="GUI task is stopped; exclusive test operation is authorized")
    parser.add_argument("--existing-fixture-report", help="permit only the active unsaved fixture matching this prior smoke report")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(preparation_plan(), ensure_ascii=False, indent=2))
        return 0
    if not args.exclusive_confirmed:
        parser.error("--execute additionally requires --exclusive-confirmed after stopping other tasks.")
    require_safe_solidworks_state(args.existing_fixture_report)
    output = ROOT / "artifacts" / "v511-live-circle-repair" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"status": "running", "steps": [], "plan": preparation_plan(),
                              "external_model_requests": 0, "existing_cad_files_opened": 0, "cad_files_saved": 0}
    worker = SolidWorksWorkerClient()
    started = time.monotonic()
    try:
        with worker.operation():
            run_fixture(worker, report)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, message=str(exc),
                      traceback=traceback.format_exc(),
                      safety="Stopped further fixture writes; preserve the new unsaved model for inspection.")
    finally:
        # Kill only our isolated helper, never ask the CAD application to quit.
        worker.interrupt()
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        path = output / "result.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps({"status": report["status"], "phase": report.get("phase"),
                          "message": report.get("message"), "report_path": str(path)}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
