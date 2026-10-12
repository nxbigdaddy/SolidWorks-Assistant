"""Opt-in read-only verification against a known locally-created synthetic cube."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.live_circle_repair_smoke import geometry_state, matches_fixture_checkpoint, same_state
from sw_assistant.solidworks.gateway import SolidWorksGateway, _read_member


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--fixture-task")
    args = parser.parse_args(argv)
    if not args.execute:
        print("prepared_not_executed: readonly 10 mm cube extents; no CAD connection")
        return 0
    if not args.fixture_task:
        parser.error("--execute requires the known synthetic cube --fixture-task report")
    path = Path(args.fixture_task).resolve(strict=True)
    if not path.is_relative_to((ROOT / "artifacts" / "configured-agent-smoke").resolve()) or path.parent.name != "tasks":
        raise RuntimeError("Fixture task must be in the local configured synthetic smoke artifacts")
    summary = json.loads((path.parent.parent / "result.json").read_text(encoding="utf-8"))
    if (summary.get("real_cad") is not True or summary.get("synthetic_data_only") is not True
            or summary.get("cad_files_saved") != 0 or summary.get("scenario") != "cube"):
        raise RuntimeError("Not an authorized unsaved synthetic cube report")
    task = json.loads(path.read_text(encoding="utf-8"))
    expected = task["latest_observation"]["snapshot"]
    if (not task["latest_observation"]["complete"] or expected["document_path"]
            or expected["document_type"] != "part" or expected["body_count"] != 1
            or expected["active_sketch"] or not math.isclose(expected["volume_m3"], 1e-6, rel_tol=1e-7)):
        raise RuntimeError("Fixture report does not contain a completed unsaved 10 mm cube")

    import pythoncom
    from win32com.client import GetActiveObject

    pythoncom.CoInitialize()
    gateway = SolidWorksGateway()
    app = document = None
    report = {"status": "running", "cad_files_saved": 0, "cad_mutations": 0, "external_requests": 0}
    started = time.monotonic()
    try:
        app = GetActiveObject("SldWorks.Application")
        document = _read_member(app, "ActiveDoc")
        if (document is None or _read_member(document, "GetPathName")
                or _read_member(document, "GetTitle") != expected["document_title"]):
            raise RuntimeError("Current document is not the expected synthetic fixture")
        gateway._sw = app
        if not matches_fixture_checkpoint(expected, gateway.snapshot().to_dict()):
            raise RuntimeError("Synthetic fixture changed since its task checkpoint")
        before = gateway._observe_model()
        report["before"] = before
        if not before["complete"]:
            raise RuntimeError("Before-observation is incomplete")
        measurement = gateway._measure_extents()
        report["measurement"] = measurement
        after = gateway._observe_model()
        report["after"] = after
        if (not after["complete"] or not same_state(geometry_state(before), geometry_state(after))
                or before["selection"] != after["selection"]):
            raise RuntimeError("Model identity, geometry, or selection changed while reading")
        if (measurement["complete"] is not True or measurement["document_identity"] != before["document_identity"]
                or not all(math.isclose(measurement["extents_mm"][axis], 10.0, rel_tol=1e-7, abs_tol=1e-6)
                           for axis in ("x", "y", "z"))):
            raise RuntimeError("GetExtremePoint measurement did not verify 10 x 10 x 10 mm")
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc), traceback=traceback.format_exc())
    finally:
        gateway.release()
        document = app = None
        pythoncom.CoUninitialize()
    report["duration_seconds"] = round(time.monotonic() - started, 2)
    output = ROOT / "artifacts" / "live-extents-readonly" / f"{time.time_ns()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output),
                      "duration_seconds": report["duration_seconds"], "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
