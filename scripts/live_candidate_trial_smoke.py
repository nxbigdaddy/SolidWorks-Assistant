"""Opt-in native candidate-copy acceptance; fixed local backend, no model requests.

Defaults to GetActiveObject; --start-solidworks explicitly permits launch.
All geometry and candidate saves belong to
new test documents. Existing document headers/update stamps stay local and are
never written into the report or task store. No document is closed or deleted.
The time budget is checked between calls; a synchronous COM call is not killed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sw_assistant.agent.models import ProposedToolCall
from sw_assistant.agent.task_models import GoalCheck, RequirementCheck, TaskDecision, TrialSpec
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import ModelSnapshot, StepResult, ToolCall, ToolError
from sw_assistant.solidworks.gateway import SolidWorksGateway, _read_member
from sw_assistant.solidworks.serial_gate import solidworks_serial_gate
from sw_assistant.tools import build_tool_registry


BASE_VOLUME = 7.0 * 9.0 * 11.0
FINAL_VOLUME = BASE_VOLUME - 5.0 * math.pi
EXPECTED_TRIALS = ["restored", "committed", "committed"]


def goal(name: str, metric: str, expected: float, *, target: str = "") -> GoalCheck:
    return GoalCheck(id=name, description=name, metric=metric, expected=expected, target=target,
                     tolerance=0.001 if metric == "volume_mm3" else 0.00001)


def step(tool: str, **arguments: Any) -> ProposedToolCall:
    return ProposedToolCall(tool=tool, arguments=arguments)


def final_goals() -> list[GoalCheck]:
    return [goal("final_volume", "volume_mm3", FINAL_VOLUME), goal("one_body", "body_count", 1),
            *[goal(f"extent_{axis}", "extent_mm", extent, target=axis)
              for axis, extent in zip(("x", "y", "z"), (7, 9, 11), strict=True)]]


class FixtureBackend:
    """Exactly five deterministic decisions; unexpected feedback stops the run."""

    def __init__(self) -> None:
        self.rounds = 0

    def decide(self, context: dict[str, Any]) -> TaskDecision:
        self.rounds += 1
        history = [item.get("status") for item in context.get("trial_history", [])]
        expected = [[], [], ["restored"], ["restored", "committed"], EXPECTED_TRIALS]
        if self.rounds > 5 or history != expected[self.rounds - 1]:
            return TaskDecision(action="review", review_kind="safety", review_critical=True,
                                reason="固定验收流程遇到非预期试验状态，保留现场，不继续尝试。")
        if self.rounds == 1:
            goals = final_goals()
            requirements = [RequirementCheck(id=item["id"], goal_ids=[g.id for g in goals])
                            for item in context.get("requirements", [])]
            return TaskDecision(action="act", reason="单独创建测试新零件，现有用户文档不参与建模。",
                                steps=[step("document.new_part")], goals=goals, requirements=requirements)
        if self.rounds == 5:
            return TaskDecision(action="complete", reason="三个候选状态符合预期，请独立验收最终几何。")
        if not any(item.get("tool") == "document.new_part" for item in context.get("completed_steps", [])):
            return TaskDecision(action="review", review_kind="safety", review_critical=True,
                                reason="测试新零件没有完成初始化，不执行候选建模。")
        if self.rounds in {2, 3}:
            width = 10 if self.rounds == 2 else 7
            steps = [step("sketch.begin", plane="front"),
                     step("sketch.rectangle", width_mm=width, height_mm=9),
                     step("feature.extrude", depth_mm=11)]
            local_volume = BASE_VOLUME
            hypothesis = ("故意使用错误宽度 10，预期体积检查失败并恢复空白基线。" if width == 10
                          else "将宽度改为 7，使用相同体积检查，预期采用正确候选。")
        else:
            # FeatureCut4 defaults opposite the sketch normal, unlike a boss.
            # Reverse=True cuts into this +normal boss from the same front plane.
            # https://help.solidworks.com/2025/english/api/sldworksapi/solidworks.interop.sldworks~solidworks.interop.sldworks.ifeaturemanager~featurecut4.html
            steps = [step("sketch.begin", plane="front"), step("sketch.circle", radius_mm=1),
                     step("feature.extrude", depth_mm=5, operation="cut", reverse=True)]
            local_volume = FINAL_VOLUME
            hypothesis = "复制已完成的非空基线，在副本上切除半径 1、深度 5 的孔，原基线不变。"
        return TaskDecision(action="act", reason=hypothesis, steps=steps,
                            trial=TrialSpec(hypothesis=hypothesis,
                                            checks=[goal("trial_volume", "volume_mm3", local_volume),
                                                    goal("trial_body", "body_count", 1)]))

    def close(self) -> None:
        pass


def document_header(gateway: Any, document: Any) -> dict[str, Any]:
    """Only identity/header reads; never serialise an existing user's header."""
    code = _read_member(document, "GetType")
    result = {"identity": gateway._com_identity(document), "title": _read_member(document, "GetTitle"),
              "path": _read_member(document, "GetPathName"), "type": code}
    if code == 1:
        configuration = _read_member(_read_member(document, "ConfigurationManager"), "ActiveConfiguration")
        result.update(configuration=_read_member(configuration, "Name"),
                      configuration_identity=gateway._com_identity(configuration))
    if not result["identity"] or not isinstance(result["title"], str) or not isinstance(result["path"], str):
        raise ToolError("Document header is unavailable; refusing test operations.")
    return result


def update_stamp(document: Any) -> int:
    stamp = _read_member(document, "GetUpdateStamp")
    if type(stamp) is not int or stamp < 0:
        raise ToolError("Document update stamp is unavailable; refusing test operations.")
    return stamp


class OriginalDocumentGuard:
    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.document = _read_member(gateway._sw, "ActiveDoc")
        self.header = document_header(gateway, self.document) if self.document is not None else None
        self.stamp = update_stamp(self.document) if self.document is not None else None

    def unchanged(self) -> bool:
        return self.document is None or (
            document_header(self.gateway, self.document) == self.header
            and update_stamp(self.document) == self.stamp
        )

    def require_unchanged(self) -> None:
        if not self.unchanged():
            raise ToolError("Original user document changed; stopping without further CAD operations.")

    def restore_if_safe(self, owned: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Activation only: no rebuild, save, undo, close or ownership guesses."""
        self.require_unchanged()
        if self.document is None:
            return {"attempted": False, "reason": "no_original_document", "original_unchanged": True}
        active = _read_member(self.gateway._sw, "ActiveDoc")
        if active is None:
            return {"attempted": False, "reason": "no_active_document", "original_unchanged": True}
        header = document_header(self.gateway, active)
        if header == self.header:
            return {"attempted": False, "reason": "original_already_active", "original_unchanged": True}
        if owned.get(header["identity"]) != header:
            return {"attempted": False, "reason": "active_document_not_owned", "original_unchanged": True}
        self.gateway._candidate_activate(self.document)
        self.require_unchanged()
        active = _read_member(self.gateway._sw, "ActiveDoc")
        if active is None or document_header(self.gateway, active) != self.header:
            raise ToolError("Original document activation could not be confirmed.")
        return {"attempted": True, "restored_original": True, "original_unchanged": True}


def restore_original_report(guard: OriginalDocumentGuard, owned: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Keep activation failure separate from evidence that the original changed.

    A failed activation is never retried here. The recovery read only checks the
    original object's header/update stamp, without switching any document.
    """
    report: dict[str, Any] = {"original_document_present": guard.document is not None}

    def record_original_state() -> None:
        unchanged = guard.unchanged()
        if type(unchanged) is not bool:
            raise ToolError("Original document verification did not return a known state.")
        report.update(original_unchanged=unchanged, original_state="unchanged" if unchanged else "changed")
        if not unchanged:
            report["passed"] = False

    try:
        with solidworks_serial_gate(30):
            report["restore_original"] = guard.restore_if_safe(owned)
            record_original_state()
    except Exception as exc:
        report.update(passed=False, restore_error_code=type(exc).__name__, restore_error=str(exc))
        try:
            with solidworks_serial_gate(30):
                record_original_state()
        except Exception as verification_error:
            report.update(original_unchanged=None, original_state="unknown",
                          original_verification_error_code=type(verification_error).__name__,
                          original_verification_error=str(verification_error))
    return report


class InlineWorker:
    """Production gateway and serial gate, but only this script's new documents.

    Before fixture creation, observations are synthetic and contain no data from
    the active user's document. Private candidates remain subject to native
    ownership/token/state verification in CandidateWorkspaceMixin.
    """

    candidate_workspace_enabled = True
    ALLOWED = {"document.new_part", "model.observe", "model.measure_extents", "sketch.begin",
               "sketch.rectangle", "sketch.circle", "feature.extrude", "session.capabilities",
               "model.references", "feature.inspect", "sketch.inspect"}

    def __init__(self, gateway: Any, guard: OriginalDocumentGuard, workspace: Path, deadline: float) -> None:
        self.gateway, self.guard, self.workspace, self.deadline = gateway, guard, workspace.resolve(), deadline
        self.registry = build_tool_registry()
        self.owned: dict[str, dict[str, Any]] = {}
        self.stopped = False
        self.timings: list[dict[str, Any]] = []

    def check_scope(self) -> None:
        if self.stopped or time.monotonic() >= self.deadline:
            raise ToolError("Local test stopped or reached its between-call time budget.")
        self.guard.require_unchanged()
        active = _read_member(self.gateway._sw, "ActiveDoc")
        if not self.owned:
            header = document_header(self.gateway, active) if active is not None else None
            if header != self.guard.header:
                raise ToolError("Active document changed before fixture creation.")
            return
        if active is None:
            raise ToolError("Owned fixture is no longer active.")
        header = document_header(self.gateway, active)
        if self.owned.get(header["identity"]) != header:
            raise ToolError("Active document is not an unchanged-header test document.")

    def adopt_active(self, *, identity: str | None = None, new_part: bool = False) -> None:
        active = _read_member(self.gateway._sw, "ActiveDoc")
        if active is None:
            raise ToolError("New test document is not active.")
        header = document_header(self.gateway, active)
        if header["type"] != 1 or (identity is not None and header["identity"] != identity):
            raise ToolError("New test document identity is not proven.")
        if header == self.guard.header:
            raise ToolError("Existing original document cannot become a test fixture.")
        if new_part:
            if header["path"] or self.owned:
                raise ToolError("Initial test fixture must be a new unsaved part.")
        elif not header["path"] or not Path(header["path"]).resolve().is_relative_to(self.workspace):
            raise ToolError("Candidate is outside the unique test workspace.")
        self.owned[header["identity"]] = header

    def call(self, call: ToolCall, timeout_seconds: int = 120) -> StepResult:
        started = time.monotonic()
        print(json.dumps({"rpc": "call", "tool": call.tool, "phase": "started"}), flush=True)
        if call.tool not in self.ALLOWED:
            raise ToolError("Smoke test refuses an unlisted CAD operation.")
        call = self.registry.validate(call)
        with solidworks_serial_gate(timeout_seconds):
            self.check_scope()
            if not self.owned and call.tool == "model.observe":
                snapshot = ModelSnapshot(connected=True)
                return StepResult(True, call, "Synthetic startup; user model not observed.",
                                  data={"snapshot": snapshot.to_dict(), "complete": True, "issues": [],
                                        "features": [], "dimensions": [], "planes": [], "selection": [],
                                        "document_identity": None, "active_sketch_geometry": [],
                                        "active_sketch_geometry_complete": True}, after=snapshot)
            if not self.owned and call.tool != "document.new_part":
                raise ToolError("Create the new test document before accessing CAD geometry.")
            if self.owned and call.tool == "document.new_part":
                raise ToolError("Only one initial test source is allowed.")
            before = self.gateway.snapshot() if self.owned else ModelSnapshot(connected=True)
            try:
                message, data = self.gateway.execute(call)
                if call.tool == "document.new_part":
                    self.adopt_active(new_part=True)
                self.check_scope()
                after = self.gateway.snapshot()
                result = StepResult(True, call, message, data=data, before=before, after=after)
            except Exception as exc:
                after = None
                try:
                    if self.owned:
                        self.check_scope()
                        after = self.gateway.snapshot()
                except Exception:
                    pass
                result = StepResult(False, call, str(exc), error_code=type(exc).__name__,
                                    before=before, after=after)
            result.duration_ms = round((time.monotonic() - started) * 1000)
            self.timings.append({"tool": call.tool, "ok": result.ok, "duration_ms": result.duration_ms})
            print(json.dumps({"rpc": "call", "tool": call.tool, "phase": "finished",
                              "ok": result.ok, "duration_ms": result.duration_ms}), flush=True)
            return result

    def candidate_request(self, operation: str, **payload: Any) -> dict[str, Any]:
        started = time.monotonic()
        print(json.dumps({"rpc": "candidate", "operation": operation, "phase": "started"}), flush=True)
        with solidworks_serial_gate(120):
            self.check_scope()
            if not self.owned:
                raise ToolError("No test-owned source exists for candidate creation.")
            if operation == "begin" and set(payload) == {"expected", "task_id"}:
                result = self.gateway.begin_candidate(payload["expected"], payload["task_id"])
                if result.get("ok") is True:
                    self.adopt_active(identity=result["candidate"]["document_identity"])
            elif (operation == "finish" and set(payload) == {"expected", "token", "accept"}
                  and type(payload["accept"]) is bool):
                result = self.gateway.finish_candidate(payload["token"], payload["accept"], payload["expected"])
            else:
                raise ToolError("Invalid private candidate request.")
            self.guard.require_unchanged()
            print(json.dumps({"rpc": "candidate", "operation": operation, "phase": "finished",
                              "ok": result.get("ok"), "status": result.get("status"),
                              "duration_ms": round((time.monotonic() - started) * 1000)}), flush=True)
            return result

    def interrupt(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.stopped = True  # Never close SolidWorks or any test/user document.

    def capture_update_marker(self) -> dict[str, Any]:
        """Bind independent measurements to the same unchanged native model."""
        with solidworks_serial_gate(30):
            self.check_scope()
            document = _read_member(self.gateway._sw, "ActiveDoc")
            if document is None or not self.owned:
                raise ToolError("Independent acceptance requires a test-owned active part.")
            header = document_header(self.gateway, document)
            stamp = update_stamp(document)
            active = _read_member(self.gateway._sw, "ActiveDoc")
            if (active is None or self.gateway._com_identity(active) != header["identity"]
                    or document_header(self.gateway, document) != header or update_stamp(document) != stamp):
                raise ToolError("Document changed while reading its independent acceptance marker.")
            return {"document_identity": header["identity"], "configuration": header["configuration"],
                    "configuration_identity": header["configuration_identity"], "update_stamp": stamp}


def independent_acceptance(before: dict[str, Any], measurement: dict[str, Any],
                           after: dict[str, Any], *, markers: list[dict[str, Any]]) -> dict[str, Any]:
    def near(value: Any, expected: float, tolerance: float = 0.00001) -> bool:
        return type(value) in (float, int) and math.isfinite(value) and abs(value - expected) <= tolerance

    snapshot = after.get("snapshot", {})
    volume = snapshot.get("volume_m3")
    volume_mm3 = volume * 1e9 if type(volume) in (float, int) and math.isfinite(volume) else None
    fields = ("document_identity", "configuration", "configuration_identity")
    marker_items = markers if isinstance(markers, list) else []
    stable_stamp = (isinstance(markers, list) and len(markers) == 4
                    and all(isinstance(item, dict) and type(item.get("update_stamp")) is int
                            and item["update_stamp"] >= 0 for item in markers)
                    and all(item["update_stamp"] == markers[0]["update_stamp"] for item in markers)
                    and all(before.get(key) and item.get(key) == before[key]
                            for item in markers for key in fields))
    checks = {"complete_observations": before.get("complete") is True and after.get("complete") is True,
              "same_document_and_configuration": all(before.get(key) and before.get(key) == measurement.get(key)
                                                       == after.get(key) for key in fields),
              "stable_update_stamp": stable_stamp,
              "one_body": all(type(value) is int and value == 1 for value in (
                  before.get("snapshot", {}).get("body_count"), snapshot.get("body_count"), measurement.get("bodies_count"))),
              "closed_sketch": before.get("snapshot", {}).get("active_sketch") == snapshot.get("active_sketch") == "",
              "healthy_features": bool(after.get("features")) and all(
                  type(item.get("error_code")) is int and item["error_code"] == 0 for item in after["features"]),
              "volume": near(volume_mm3, FINAL_VOLUME, 0.001),
              "native_extents": measurement.get("complete") is True and measurement.get("issues") == []
              and measurement.get("source") == "IBody2.GetExtremePoint"
              and measurement.get("coordinate_system") == "model",
              **{f"extent_{axis}": near(measurement.get("extents_mm", {}).get(axis), expected)
                 for axis, expected in zip(("x", "y", "z"), (7, 9, 11), strict=True)}}
    return {"passed": all(checks.values()), "checks": checks, "volume_mm3": volume_mm3,
            "expected_volume_mm3": FINAL_VOLUME, "extents_mm": measurement.get("extents_mm", {}),
            "update_stamps": [item["update_stamp"] if isinstance(item, dict)
                              and type(item.get("update_stamp")) is int and item["update_stamp"] >= 0
                              else None for item in marker_items]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Allow creating new native test documents/copies.")
    parser.add_argument("--start-solidworks", action="store_true", help="Explicitly permit launching SolidWorks if absent.")
    parser.add_argument("--exclusive-confirmed", action="store_true", help="No other CAD task or user interaction.")
    parser.add_argument("--budget-seconds", type=int, default=1800, help="Between-call budget; COM calls are synchronous.")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required; nothing has been attached or created.")
    if not 30 <= args.budget_seconds <= 3600:
        parser.error("budget must be 30..3600 seconds")
    started = time.monotonic()
    output = ROOT / "artifacts" / "candidate-trial-smoke" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"passed": False, "status": "starting", "external_model_requests": 0,
                              "original_geometry_read": False, "original_saved_or_closed": False,
                              "start_solidworks_permitted": args.start_solidworks,
                              "artifacts": str(output), "trial_statuses": [], "events": []}
    gateway = guard = worker = runner = pythoncom = None
    initialized = False

    def progress(name: str, payload: dict[str, Any]) -> None:
        if name.startswith("trial_") or name in {"step_started", "step_succeeded", "task_review", "task_completed"}:
            entry = {"event": name, "elapsed_seconds": round(time.monotonic() - started, 2),
                     "tool": payload.get("call", {}).get("tool", "")}
            report["events"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)

    try:
        import pythoncom
        from win32com.client import GetActiveObject

        pythoncom.CoInitialize()
        initialized = True
        gateway = SolidWorksGateway(visible=True)
        with solidworks_serial_gate(30):
            try:
                gateway._sw = GetActiveObject("SldWorks.Application")
            except Exception:
                if not args.start_solidworks:
                    raise ToolError("SolidWorks is not running; launching requires --start-solidworks.") from None
                print(json.dumps({"phase": "starting_solidworks", "explicit_launch_permission": True}), flush=True)
                gateway.connect()  # Explicitly opted in; never handle licence dialogs.
            report["solidworks_started_by_test"] = gateway._owns_application
            guard = OriginalDocumentGuard(gateway)
            guard.require_unchanged()
            gateway.configure_candidate_workspace(output / "candidates")
        worker = InlineWorker(gateway, guard, output / "candidates", started + args.budget_seconds)
        backend = FixtureBackend()
        runner = ContinuousTaskRunner(build_tool_registry(), worker, backend, TaskStore(output / "tasks"),
                                      on_event=progress, max_rounds=6)
        result = runner.prepare("创建 7×9×11 毫米测试实体，并从底部切除半径 1 毫米深 5 毫米的孔")
        if result.get("status") == "ready":
            result = runner.run()
        report.update(status=result.get("status"), rounds=result.get("rounds"),
                      trial_statuses=[item.get("status") for item in result.get("trial_history", [])],
                      trial_checks=[item.get("checks", []) for item in result.get("trial_history", [])],
                      verification=result.get("verification", {}), message=result.get("message"))
        if result.get("status") == "completed":
            # Independent oracle: fresh configuration-bound observation, native
            # body extrema, then another observation in the same configuration.
            # Four native update markers also reject same-volume shape changes.
            markers = [worker.capture_update_marker()]
            before = worker.call(ToolCall("model.observe", {"include_configuration": True}))
            markers.append(worker.capture_update_marker())
            measurement = worker.call(ToolCall("model.measure_extents", {}))
            markers.append(worker.capture_update_marker())
            after = worker.call(ToolCall("model.observe", {"include_configuration": True}))
            markers.append(worker.capture_update_marker())
            acceptance = independent_acceptance(before.data, measurement.data, after.data, markers=markers)
            acceptance["passed"] = bool(acceptance["passed"] and before.ok and measurement.ok and after.ok)
            report["independent_acceptance"] = acceptance
        report["passed"] = (result.get("status") == "completed" and backend.rounds == 5
                            and report["trial_statuses"] == EXPECTED_TRIALS
                            and report.get("independent_acceptance", {}).get("passed") is True)
    except Exception as exc:
        report.update(status="failed", error_code=type(exc).__name__, error=str(exc))
    finally:
        if runner is not None:
            current = runner.snapshot()
            report.setdefault("trial_checks", [item.get("checks", []) for item in current.get("trial_history", [])])
            if not report["trial_statuses"]:
                report["trial_statuses"] = [item.get("status") for item in current.get("trial_history", [])]
        if worker is not None:
            report["call_timings"] = worker.timings
            report["retained_test_document_count"] = len(worker.owned)
        if guard is not None:
            report.update(restore_original_report(guard, worker.owned if worker else {}))
        else:
            report.update(passed=False, original_unchanged=None, original_state="unknown")
        if gateway is not None:
            gateway.release(close_owned=False)
        if initialized and pythoncom is not None:
            pythoncom.CoUninitialize()
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["retained_candidate_files"] = [str(path) for path in (output / "candidates").rglob("*.sldprt")]
        # Exception strings can include an existing document title/path. Redact
        # those private header fields even though they were never sent to CAD tools.
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if guard is not None and guard.header:
            for key in ("path", "title"):
                private = guard.header.get(key)
                if isinstance(private, str) and private:
                    text = text.replace(json.dumps(private, ensure_ascii=False)[1:-1], "[original-document]")
        (output / "result.json").write_text(text, encoding="utf-8")
        print(f"Evidence: {output / 'result.json'}", flush=True)
        print(f"Passed: {report['passed']}; duration: {report['duration_seconds']} seconds", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
