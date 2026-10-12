"""Opt-in, local fixed-backend acceptance through the production spawned CAD worker.

This is NOT a Pi/model-autonomy test. It uses the same ContinuousTaskRunner,
SolidWorksWorkerClient, spawn context, worker_main and checked_call as the app.
The only direct COM calls are startup connection/document-count checks and the
final document-count check. All modeling and geometric verification use RPC.

No open document is accepted at startup; close your documents yourself before
running. Do not interact with SolidWorks or run another CAD task during the test.
No document is closed, deleted or undone. All four test documents and candidate
evidence are retained; explicit checkpoint-restore verification opens a fifth
independent document and never automatically continues the restored task.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.live_candidate_trial_smoke import EXPECTED_TRIALS, FixtureBackend, independent_acceptance
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import StepResult, ToolCall, ToolError
from sw_assistant.solidworks.client import SolidWorksWorkerClient
from sw_assistant.solidworks.gateway import SolidWorksGateway, _read_member
from sw_assistant.solidworks.serial_gate import solidworks_serial_gate
from sw_assistant.tools import build_tool_registry


BUDGET_SECONDS = 1800
RPC_TIMEOUT_SECONDS = 120
EXPECTED_DOCUMENTS = 4
NOT_RUNNING_HRESULT = -2147221021  # MK_E_UNAVAILABLE, not an arbitrary COM error.


def document_count(application: Any) -> int:
    """Read counts only; never inspect another document's name or geometry."""
    count = _read_member(application, "GetDocumentCount")
    documents = _read_member(application, "GetDocuments")
    if type(count) is not int or count < 0 or not (documents is None or isinstance(documents, (list, tuple))):
        raise ToolError("Cannot prove SolidWorks document enumeration; refusing this test.")
    if len(documents or ()) != count:
        raise ToolError("SolidWorks document count changed during preflight; refusing this test.")
    return count


def require_empty_session(application: Any) -> None:
    count = document_count(application)
    active = _read_member(application, "ActiveDoc")
    if count != 0 or active is not None:
        raise ToolError("SolidWorks has open documents. Close them yourself before this isolated test; nothing was changed.")


def attach_empty_session(gateway: SolidWorksGateway, *, allow_start: bool, get_active: Any) -> bool:
    """Only a proven absent ROT object plus explicit flag permits launch."""
    try:
        gateway._sw = get_active("SldWorks.Application")
    except Exception as exc:
        if getattr(exc, "hresult", None) != NOT_RUNNING_HRESULT:
            raise ToolError("The running SolidWorks session could not be checked; no launch was attempted.") from None
        if not allow_start:
            raise ToolError("SolidWorks is not running. Starting it requires --start-solidworks.") from None
        gateway.connect()
    require_empty_session(gateway._sw)
    return bool(gateway._owns_application)


def observation_marker(result: StepResult) -> dict[str, Any]:
    data = result.data
    if not result.ok or data.get("complete") is not True:
        raise ToolError("Independent production observation is failed or incomplete.")
    stamp = data.get("model_update_stamp")
    fields = ("document_identity", "configuration", "configuration_identity")
    if type(stamp) is not int or stamp < 0 or any(not isinstance(data.get(key), str) or not data[key] for key in fields):
        raise ToolError("Independent production observation has no reliable configuration/update marker.")
    return {**{key: data[key] for key in fields}, "update_stamp": stamp}


def production_acceptance(worker: SolidWorksWorkerClient, check_budget: Any) -> dict[str, Any]:
    """Four fresh, worker-owned update observations enclose native extent RPC.

    No gateway or host COM geometry shortcuts. Every read goes through the real
    production client's ordinary call path; modeling remains runner checked_call.
    """
    observations: list[StepResult] = []
    measurement: StepResult | None = None
    timings: list[dict[str, Any]] = []
    for index in range(5):
        check_budget()
        call = ToolCall("model.measure_extents", {}) if index == 2 else ToolCall(
            "model.observe", {"include_configuration": True},
        )
        started = time.monotonic()
        result = worker.call(call, RPC_TIMEOUT_SECONDS)
        check_budget()
        timings.append({"tool": call.tool, "ok": result.ok,
                        "host_duration_ms": round((time.monotonic() - started) * 1000),
                        "worker_duration_ms": result.duration_ms})
        if index == 2:
            measurement = result
        else:
            observations.append(result)
    markers = [observation_marker(item) for item in observations]
    if measurement is None:
        raise ToolError("Independent native extent measurement is missing.")
    acceptance = independent_acceptance(observations[1].data, measurement.data, observations[2].data, markers=markers)
    # The outer observations are independently checked too, not merely used as
    # stamps: an incomplete or contradictory final snapshot cannot be ignored.
    individual = [independent_acceptance(item.data, measurement.data, item.data, markers=markers)
                  for item in observations]
    acceptance.update(observation_checks=[item["checks"] for item in individual], timings=timings,
                      production_rpc=True, observation_count=len(observations))
    acceptance["passed"] = bool(acceptance["passed"] and all(item["passed"] for item in individual) and measurement.ok)
    return acceptance


def checkpoint_signature(response: dict[str, Any], checkpoint_id: str) -> tuple[str, int]:
    """The production list RPC rehashes native files; no caller-supplied path."""
    if (response.get("ok") is not True or response.get("status") != "listed"
            or response.get("issues") != [] or not isinstance(response.get("checkpoints"), list)):
        raise ToolError("Checkpoint catalog is incomplete or contains integrity issues.")
    matches = [item for item in response["checkpoints"]
               if isinstance(item, dict) and item.get("checkpoint_id") == checkpoint_id]
    if len(matches) != 1 or matches[0].get("integrity") != "verified":
        raise ToolError("The selected host checkpoint has no unique verified native artifact.")
    digest, size = matches[0].get("native_sha256"), matches[0].get("native_size")
    if (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or type(size) is not int or size <= 0):
        raise ToolError("Checkpoint digest or native size is invalid.")
    return digest, size


def verify_checkpoint_restore(runner: ContinuousTaskRunner, worker: SolidWorksWorkerClient,
                              check_budget: Any) -> dict[str, Any]:
    """Explicitly restore latest accepted checkpoint, then independently measure.

    The caller keeps its outer production operation epoch. The nested scope does
    not reset it after cancellation. There is deliberately no runner.run here.
    """
    check_budget()
    before = runner.snapshot()
    records = before.get("checkpoint_records")
    checkpoint_id, task_id = before.get("active_checkpoint_id"), before.get("id")
    if (before.get("status") != "completed" or not isinstance(records, list) or not records
            or not isinstance(records[-1], dict) or records[-1].get("checkpoint_id") != checkpoint_id
            or records[-1].get("task_id") != task_id or not isinstance(checkpoint_id, str)):
        raise ToolError("Completed task has no latest host-bound checkpoint eligible for this acceptance.")
    report: dict[str, Any] = {"requested": True, "passed": False, "checkpoint_id": checkpoint_id,
                              "automatic_run_called": False}
    with worker.operation():
        signature_before = checkpoint_signature(worker.checkpoint_request("list", task_id=task_id), checkpoint_id)
        check_budget()
        restored = runner.restore_checkpoint(checkpoint_id)
        check_budget()
        report.update(status=restored.get("status"), message=restored.get("message"),
                      native_sha256_before=signature_before[0], native_size_before=signature_before[1])
        if restored.get("status") != "ready":
            return report  # Unknown restore state must not cause another CAD operation.
        checks = {
            "ready_without_automatic_run": restored.get("pending_decision") is None and restored.get("pending_steps") == [],
            "same_goals": before.get("goals") == restored.get("goals"),
            "same_requirements": before.get("requirements") == restored.get("requirements"),
            "same_action_and_round_budget": all(before.get(key) == restored.get(key) for key in ("actions", "rounds")),
            "selected_checkpoint": restored.get("active_checkpoint_id") == checkpoint_id,
        }
        report["checks"] = checks
        if not all(checks.values()):
            return report
        report["independent_acceptance"] = production_acceptance(worker, check_budget)
        signature_after = checkpoint_signature(worker.checkpoint_request("list", task_id=task_id), checkpoint_id)
        check_budget()
        report.update(native_sha256_after=signature_after[0], native_size_after=signature_after[1])
        checks["immutable_checkpoint"] = signature_before == signature_after
        report["passed"] = all(checks.values()) and report["independent_acceptance"]["passed"] is True
    return report


class BudgetGuard:
    """One total host budget; cancellation must not start a replacement worker."""

    def __init__(self, started: float) -> None:
        self.deadline = started + BUDGET_SECONDS
        self.expired = threading.Event()
        self.worker: SolidWorksWorkerClient | None = None
        self.runner: ContinuousTaskRunner | None = None
        self.timer = threading.Timer(max(0, self.deadline - time.monotonic()), self.cancel)
        self.timer.daemon = True

    def cancel(self) -> None:
        self.expired.set()
        print(json.dumps({"event": "total_budget_exhausted", "budget_seconds": BUDGET_SECONDS}), flush=True)
        if self.runner is not None:
            self.runner.stop()
        elif self.worker is not None:
            self.worker.interrupt()

    def check(self) -> None:
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            if not self.expired.is_set():
                self.cancel()
            raise ToolError("Total 1800-second host budget exceeded. Execution outcome may be unknown; no retry was attempted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly permit new test documents and files.")
    parser.add_argument("--start-solidworks", action="store_true", help="Explicitly permit launch only when not running.")
    parser.add_argument("--verify-checkpoint-restore", action="store_true",
                        help="Also explicitly restore the latest accepted checkpoint into a fifth document; never auto-run.")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required; no SolidWorks attachment, process or files were created.")

    started = time.monotonic()
    output = ROOT / "artifacts" / "production-reliability-smoke" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "passed": False, "status": "starting", "backend": "fixed_local_fixture_not_pi",
        "execution_path": "ContinuousTaskRunner -> SolidWorksWorkerClient -> spawn -> worker_main -> SolidWorksGateway",
        "external_model_requests": 0, "host_geometry_calls": 0, "documents_closed_or_deleted": 0,
        "budget_seconds": BUDGET_SECONDS, "ordinary_rpc_timeout_seconds": RPC_TIMEOUT_SECONDS,
        "candidate_begin_timeout_seconds": 180, "candidate_finish_timeout_seconds": 240,
        "budget_covers": "whole run; watchdog interrupts production worker, not SolidWorks; startup header COM reads are synchronous",
        "start_solidworks_permitted": args.start_solidworks, "events": [], "rpc_events": [],
        "checkpoint_restore_requested": args.verify_checkpoint_restore,
        "expected_open_document_count": EXPECTED_DOCUMENTS + int(args.verify_checkpoint_restore),
        "trial_statuses": [], "artifacts": str(output),
    }
    budget = BudgetGuard(started)
    gateway = worker = runner = None
    com_runtime: Any = None
    initialized = False

    def progress(name: str, payload: dict[str, Any]) -> None:
        if name.startswith(("trial_", "checkpoint_")) or name in {"step_started", "step_succeeded", "task_review", "task_completed"}:
            entry = {"event": name, "elapsed_seconds": round(time.monotonic() - started, 2),
                     "tool": payload.get("call", {}).get("tool", "")}
            report["events"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)

    def rpc_progress(event: str, payload: dict[str, Any]) -> None:
        # Host client hook only: no proxy/subclass, no monkeypatching call_checked,
        # no argument/model/header contents copied into public progress.
        safe: dict[str, Any] = {key: payload[key] for key in ("request_id", "kind", "tool", "operation", "phase", "ok", "duration_ms",
                                                             "elapsed_seconds", "timeout_seconds", "error_code") if key in payload}
        safe.update(event=event, elapsed_run_seconds=round(time.monotonic() - started, 2))
        report["rpc_events"].append(safe)
        print(json.dumps(safe, ensure_ascii=False), flush=True)

    budget.timer.start()
    try:
        import pythoncom
        from win32com.client import GetActiveObject

        com_runtime = pythoncom
        com_runtime.CoInitialize()
        initialized = True
        gateway = SolidWorksGateway(visible=True)
        with solidworks_serial_gate(30):
            budget.check()
            report["solidworks_started_by_test"] = attach_empty_session(
                gateway, allow_start=args.start_solidworks, get_active=GetActiveObject,
            )
            report["startup_existing_document_count"] = 0
        budget.check()
        worker = SolidWorksWorkerClient(candidate_workspace=output / "candidates", on_progress=rpc_progress,
                                       allow_start_solidworks=False)
        budget.worker = worker
        backend = FixtureBackend()
        runner = ContinuousTaskRunner(build_tool_registry(), worker, backend, TaskStore(output / "tasks"),
                                      on_event=progress, max_rounds=6)
        budget.runner = runner
        # Bind every runner RPC and independent read to one cancellation epoch.
        # A timed-out worker must never be silently replaced during this test.
        with worker.operation():
            result = runner.prepare("创建 7×9×11 毫米测试实体，并从底部切除半径 1 毫米深 5 毫米的孔")
            with solidworks_serial_gate(30):
                budget.check()
                require_empty_session(gateway._sw)
            if result.get("status") == "ready":
                result = runner.run()
            report.update(status=result.get("status"), rounds=result.get("rounds"),
                          backend_rounds=backend.rounds,
                          trial_statuses=[item.get("status") for item in result.get("trial_history", [])],
                          trial_checks=[item.get("checks", []) for item in result.get("trial_history", [])],
                          verification=result.get("verification", {}), message=result.get("message"),
                          checkpoints=result.get("checkpoints", []))
            budget.check()
            if result.get("status") == "completed":
                report["independent_acceptance"] = production_acceptance(worker, budget.check)
                if args.verify_checkpoint_restore and report["independent_acceptance"]["passed"]:
                    report["checkpoint_restore"] = verify_checkpoint_restore(runner, worker, budget.check)
        restored_as_requested = (not args.verify_checkpoint_restore
                                 or report.get("checkpoint_restore", {}).get("passed") is True)
        # A failure/timeout must not trigger another direct COM probe against an
        # unknown or still-busy session. Leave the retained count unknown then.
        if result.get("status") == "completed" and restored_as_requested:
            with solidworks_serial_gate(30):
                budget.check()
                report["retained_open_document_count"] = document_count(gateway._sw)
        report["passed"] = (result.get("status") == "completed" and backend.rounds == 5
                            and report["trial_statuses"] == EXPECTED_TRIALS
                            and report.get("independent_acceptance", {}).get("passed") is True
                            and restored_as_requested
                            and report.get("retained_open_document_count") == report["expected_open_document_count"])
    except Exception as exc:
        report.update(status="failed", error_code=type(exc).__name__, error=str(exc))
    finally:
        budget.timer.cancel()
        if runner is not None:
            current = runner.snapshot()
            report.update(task_id=current.get("id"), task_status=current.get("status"),
                          task_message=current.get("message"), feedback=current.get("feedback", []),
                          active_trial=current.get("active_trial"))
            report["trial_statuses"] = [item.get("status") for item in current.get("trial_history", [])]
            report["checkpoints"] = current.get("checkpoints", report.get("checkpoints", []))
            report["checkpoint_records"] = [
                {key: item.get(key) for key in ("task_id", "checkpoint_id", "parent_id", "created_at")}
                for item in current.get("checkpoint_records", []) if isinstance(item, dict)
            ]
        if worker is not None:
            worker.close()  # Ends helper only; does not close any SolidWorks document.
        if gateway is not None:
            gateway.release(close_owned=False)
        if initialized and com_runtime is not None:
            com_runtime.CoUninitialize()
        report["total_budget_exhausted"] = budget.expired.is_set() or time.monotonic() >= budget.deadline
        if report["total_budget_exhausted"]:
            report.update(passed=False, status="budget_exhausted")
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["retained_candidate_files"] = [str(path) for path in (output / "candidates").rglob("*.sldprt")]
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(f"Evidence: {output / 'result.json'}", flush=True)
        print(f"Passed: {report['passed']}; duration: {report['duration_seconds']} seconds", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
