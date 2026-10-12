"""Opt-in configured-model smoke: synthetic prompt only, optional new unsaved CAD."""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sw_assistant.agent.task_backend import TaskAgentBackend
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import ModelSnapshot, StepResult, ToolCall
from sw_assistant.infrastructure.paths import AppPaths
from sw_assistant.infrastructure.settings import SettingsStore
from sw_assistant.solidworks import SolidWorksWorkerClient
from sw_assistant.tools import build_tool_registry


PROMPTS = {
    "cube": "新建一个边长 10 mm 的实心正方体。",
    "phone": "画一个 iphone 14 promax 手机，作为简化外观概念模型。",
}


class SyntheticUnconnectedCad:
    """Plan-only fixture. Never imports COM or accesses local CAD data."""

    def call(self, call: ToolCall, timeout_seconds: int = 120) -> StepResult:
        snapshot = ModelSnapshot()
        if call.tool in {"model.observe", "model.inspect"}:
            data = {"snapshot": snapshot.to_dict(), "complete": True, "features": [],
                    "dimensions": [], "planes": [], "issues": []}
        elif call.tool == "session.capabilities":
            data = {"connected": False, "document_type": "none", "complete": True,
                    "message": "Synthetic disconnected session; no CAD process exists."}
        elif call.tool in {"model.references", "model.measure_extents", "sketch.inspect", "feature.inspect"}:
            return StepResult(False, call, "当前未连接 SolidWorks，尚无活动文档。",
                              error_code="NoActiveDocument", before=snapshot, after=snapshot)
        else:
            raise AssertionError("Plan-only fixture cannot execute CAD writes.")
        return StepResult(True, call, "Synthetic read-only evidence", data=data,
                          before=snapshot, after=snapshot)

    def interrupt(self) -> None:
        pass

    def close(self) -> None:
        pass


class OwnedFixtureCad:
    """Expose only a freshly created test document to the external model.

    All observations used to check ownership remain local. A switched active
    document stops the test before its data is returned to the Agent.
    """

    def __init__(self, worker: SolidWorksWorkerClient) -> None:
        self.worker = worker
        self.identity: str | None = None
        self.title: str = ""
        self.timings: list[dict[str, Any]] = []

    def create(self) -> None:
        for name in ("session.ensure", "document.new_part"):
            result = self.worker.call(ToolCall(name, {}))
            if not result.ok:
                raise RuntimeError(f"Cannot create isolated test fixture: {result.error_code}")
        if result.after is None or not result.after.document_title:
            raise RuntimeError("New document result did not include its title; test stopped.")
        self._adopt_new(result.after.document_title)

    def _adopt_new(self, expected_title: str | None = None) -> None:
        observed = self.worker.probe_document_identity()
        identity = observed.get("document_identity")
        if (not isinstance(identity, str) or not identity or observed.get("document_path")
                or (expected_title is not None and observed.get("document_title") != expected_title)):
            raise RuntimeError("New unsaved fixture could not be identified; test stopped.")
        self.identity = identity
        self.title = str(observed.get("document_title") or "")

    def call(self, call: ToolCall, timeout_seconds: int = 120) -> StepResult:
        if not isinstance(self.identity, str) or not self.identity or not self.title:
            raise RuntimeError("Test fixture ownership has not been established.")
        started = time.monotonic()
        observed = self.worker.probe_document_identity()
        before_done = time.monotonic()
        if (observed.get("document_identity") != self.identity or observed.get("document_path")):
            raise RuntimeError("Active CAD document changed; no user CAD data was returned to the model.")
        result = self.worker.call(call, timeout_seconds)
        operation_done = time.monotonic()
        if result.before is not None and (result.before.document_path or result.before.document_title != self.title):
            raise RuntimeError("Document changed during CAD call; result withheld from model.")
        if call.tool == "document.new_part" and result.ok:
            if result.after is None or not result.after.document_title:
                raise RuntimeError("New document result lacks ownership evidence.")
            self._adopt_new(result.after.document_title)
        after = self.worker.probe_document_identity()
        if (after.get("document_identity") != self.identity or after.get("document_path")
                or (result.after is not None and (
                    result.after.document_path or result.after.document_title != self.title))):
            raise RuntimeError("Document changed during CAD call; result withheld from model.")
        self.timings.append({"tool": call.tool, "ok": result.ok,
                             "guard_before_ms": round((before_done - started) * 1000),
                             "operation_ms": result.duration_ms,
                             "guard_after_ms": round((time.monotonic() - operation_done) * 1000),
                             "total_ms": round((time.monotonic() - started) * 1000)})
        return result

    def interrupt(self) -> None:
        self.worker.interrupt()

    def close(self) -> None:
        # Preserve the unsaved test scene. A graceful owned-application release
        # is deliberately not requested by this smoke test.
        self.worker.interrupt()


def check_cube_acceptance(observation: dict[str, Any], measurement: dict[str, Any]) -> dict[str, Any]:
    """Independent fixture oracle; an Agent's completion claim is insufficient."""
    def near(value: Any, expected: float, tolerance: float) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and abs(value - expected) <= tolerance)

    snapshot = observation.get("snapshot", {})
    identity = observation.get("document_identity")
    features = observation.get("features", [])
    extents = measurement.get("extents_mm", {})
    checks = {
        "complete_observation": observation.get("complete") is True,
        "same_document": bool(identity) and measurement.get("document_identity") == identity,
        "unsaved_part": snapshot.get("document_type") == "part" and snapshot.get("document_path") == "",
        "one_solid_body": snapshot.get("body_count") == 1 and measurement.get("bodies_count") == 1,
        "sketch_closed": snapshot.get("active_sketch") == "",
        "features_healthy": bool(features) and all(item.get("error_code") == 0 for item in features),
        "volume_1000_mm3": near(snapshot.get("volume_mm3"), 1000.0, 0.001),
        "exact_extents_source": measurement.get("complete") is True and not measurement.get("issues")
        and measurement.get("source") == "IBody2.GetExtremePoint"
        and measurement.get("coordinate_system") == "model",
        **{f"extent_{axis}_10_mm": near(extents.get(axis), 10.0, 0.00001) for axis in ("x", "y", "z")},
    }
    return {"passed": all(checks.values()), "checks": checks, "extents_mm": extents,
            "volume_mm3": snapshot.get("volume_mm3")}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Allow requests to the locally configured model with synthetic test data.")
    parser.add_argument("--real-cad", action="store_true",
                        help="Create a new UNSAVED document and run the full task; do not use during another CAD task.")
    parser.add_argument("--exclusive-confirmed", action="store_true",
                        help="Confirm no concurrent CAD task or user CAD interaction during this smoke test.")
    parser.add_argument("--scenario", choices=tuple(PROMPTS), default="cube")
    parser.add_argument("--budget-seconds", type=int, default=360)
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required: this test calls your configured model and may consume credits.")
    if args.real_cad and not args.exclusive_confirmed:
        parser.error("--real-cad requires --exclusive-confirmed: no other CAD tasks or desktop interaction.")
    if not 30 <= args.budget_seconds <= 1800:
        parser.error("Test budget must be between 30 and 1800 seconds.")
    settings = SettingsStore(AppPaths.default().settings).load()
    settings.model.validate()
    if not settings.model.configured or settings.model.credential_unreadable:
        raise RuntimeError("Configured model is missing or its key cannot be decrypted.")
    stamp = str(time.time_ns())
    output = ROOT / "artifacts" / "configured-agent-smoke" / f"{args.scenario}-{stamp}"
    registry = build_tool_registry()
    events: list[dict[str, Any]] = []

    def progress(name: str, payload: dict[str, Any]) -> None:
        if name in {"agent_decision", "step_succeeded", "step_failed", "task_review",
                    "task_feedback", "investigation_started", "task_completed", "task_stopped"}:
            item = {"event": name, "at_seconds": round(time.monotonic() - started, 2),
                    "tool": payload.get("tool", payload.get("call", {}).get("tool", "")),
                    "message": payload.get("message", payload.get("decision", {}).get("reason", ""))}
            events.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)

    backend = TaskAgentBackend(settings.model, registry, output / "agent",
                               session_id=f"synthetic-{stamp}", backend="pi")
    raw_worker = SolidWorksWorkerClient() if args.real_cad else None
    worker = OwnedFixtureCad(raw_worker) if raw_worker else SyntheticUnconnectedCad()
    runner = ContinuousTaskRunner(registry, worker, backend, TaskStore(output / "tasks"),
                                  on_event=progress, max_rounds=20)
    backend.set_investigator(runner.investigate)
    started = time.monotonic()
    timer = threading.Timer(args.budget_seconds, runner.stop)
    timer.daemon = True
    result: dict[str, Any] = {}
    independent_acceptance: dict[str, Any] = {}
    timer.start()
    try:
        with raw_worker.operation() if raw_worker else nullcontext():
            if isinstance(worker, OwnedFixtureCad):
                worker.create()
            result = runner.prepare(PROMPTS[args.scenario])
            if args.real_cad and result.get("status") == "ready":
                result = runner.run()
            if args.real_cad and args.scenario == "cube" and result.get("status") == "completed":
                measurement = worker.call(ToolCall("model.measure_extents", {}))
                observation = worker.call(ToolCall("model.observe", {}))
                independent_acceptance = check_cube_acceptance(observation.data, measurement.data)
                if not measurement.ok or not observation.ok:
                    independent_acceptance["passed"] = False
        expected_status = "completed" if args.real_cad else "ready"
        passed = result.get("status") == expected_status
        if args.real_cad and args.scenario == "cube":
            passed = passed and independent_acceptance.get("passed") is True
        summary = {"passed": passed, "status": result.get("status"),
                   "message": result.get("message"), "model": settings.model.model,
                   "engine": backend.backend_source, "scenario": args.scenario,
                   "real_cad": args.real_cad, "synthetic_data_only": True,
                   "cad_files_saved": 0, "duration_seconds": round(time.monotonic() - started, 2),
                   "agent_progress": backend.progress,
                   "rounds": result.get("rounds"), "diagnostics": result.get("diagnostics"),
                   "completed_steps": len(result.get("completed_steps", [])),
                   "pending_steps": [s.get("tool") for s in result.get("pending_steps", [])],
                   "verification": result.get("verification", {}), "events": events,
                   "independent_acceptance": independent_acceptance,
                   "call_timings": worker.timings if isinstance(worker, OwnedFixtureCad) else []}
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({key: value for key, value in summary.items() if key not in {"events", "call_timings"}},
                         ensure_ascii=False, indent=2), flush=True)
        print(f"Evidence: {output / 'result.json'}", flush=True)
        return 0 if passed else 1
    finally:
        timer.cancel()
        backend.close()
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
