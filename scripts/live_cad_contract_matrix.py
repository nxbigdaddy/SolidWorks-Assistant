"""Explicit, bounded native CAD contract acceptance using production spawned RPC.

Default: read-only empty-session preflight. --execute creates independent
synthetic parts; --start-solidworks separately permits starting an absent app.
The worker is ALWAYS attach-only: loss of that app can never cause a restart.
No existing document is inspected beyond its count, modified, saved or closed.
All synthetic parts/candidates/checkpoint copies remain open for inspection.

The default case is front.rectangle, NOT the whole catalog. Use --case all or
repeat --case. Unselected/interrupted cases are explicitly not_run. No model,
Pi session, prompts, credentials or user CAD files are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.cad_contract_catalog import CASE_IDS, MAX_REPEATS, ContractCase, case, sketch_oracle, solid_oracle, stable_observations
from scripts.live_production_reliability_smoke import (
    BUDGET_SECONDS, RPC_TIMEOUT_SECONDS, BudgetGuard, attach_empty_session,
    checkpoint_signature, document_count, require_empty_session,
)
from sw_assistant.domain import StepResult, ToolCall, ToolError
from sw_assistant.solidworks.client import SolidWorksWorkerClient, _CANDIDATE_TIMEOUTS, _CHECKPOINT_TIMEOUTS
from sw_assistant.solidworks.gateway import SolidWorksGateway, _read_member
from sw_assistant.solidworks.serial_gate import solidworks_serial_gate
from sw_assistant.tools import build_tool_registry
from sw_assistant.validation.provenance import runtime_fingerprint, source_fingerprint


class ContractFailure(ToolError):
    pass


class BudgetBoundary(ToolError):
    """No next operation was submitted: reserve the whole RPC window first."""


class MatrixBudget(BudgetGuard):
    """Keep the original total watchdog; reserve writes without extending it.

    A soft boundary does not call cancel/interrupt: there is no pending write
    to kill. An already-fired watchdog still has the original unknown-outcome
    semantics and must not be reclassified as a harmless admission refusal.
    """

    def __init__(self, started: float) -> None:
        super().__init__(started)
        self._last_checked_clock = started

    def check(self, minimum_remaining: float = 0) -> None:
        if (isinstance(minimum_remaining, bool) or not isinstance(minimum_remaining, (int, float))
                or not math.isfinite(minimum_remaining) or minimum_remaining < 0):
            raise ValueError("minimum_remaining must be a finite nonnegative number")
        if self.expired.is_set():
            super().check()  # The watchdog already interrupted; retain unknown outcome.
        now = time.monotonic()
        if (not math.isfinite(now) or not math.isfinite(self.deadline)
                or now < self._last_checked_clock):
            raise BudgetBoundary("budget_boundary: the monotonic clock is invalid or regressed; no next RPC submitted")
        self._last_checked_clock = now
        available = self.deadline - now
        if available <= minimum_remaining:
            raise BudgetBoundary(
                f"budget_boundary: {available:.3f}s remain; more than {minimum_remaining:.3f}s "
                "are required before submitting the next operation")


WRITE_FINISH_MARGIN_SECONDS = 30


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight-only", action="store_true", help="Read-only preflight; this is the default.")
    mode.add_argument("--execute", action="store_true", help="Explicitly create synthetic parts for the selected cases.")
    parser.add_argument("--start-solidworks", action="store_true", help="Allow host launch only if proven absent; requires --execute.")
    parser.add_argument("--case", dest="cases", action="append", choices=(*CASE_IDS, "all"), help="Repeatable fixed case; default front.rectangle.")
    parser.add_argument("--repeat", type=int, choices=range(1, MAX_REPEATS + 1), default=1, help="Bounded variants with changed dimensions/positions.")
    parser.add_argument("--verify-save-reopen", action="store_true", help="Save and reopen each accepted solid via host checkpoint RPC; retain every document.")
    args = parser.parse_args(argv)
    if not args.execute and (args.start_solidworks or args.verify_save_reopen):
        parser.error("--start-solidworks and --verify-save-reopen require --execute; no CAD action was taken.")
    chosen = args.cases or ["front.rectangle"]
    if "all" in chosen and len(chosen) != 1:
        parser.error("--case all cannot be combined with individual cases.")
    args.cases = list(CASE_IDS) if chosen == ["all"] else list(dict.fromkeys(chosen))
    return args


def read(worker: Any, tool: str, check_budget: Any) -> dict[str, Any]:
    check_budget()
    call = ToolCall(tool, {"include_configuration": True} if tool == "model.observe" else {})
    result = worker.call(call, RPC_TIMEOUT_SECONDS)
    check_budget()
    if not result.ok:
        raise ContractFailure(f"Independent {tool} RPC failed: {result.error_code}: {result.message}")
    if result.data.get("complete") is not True or result.data.get("issues") != []:
        raise ContractFailure(f"Independent {tool} readback is incomplete: {result.data.get('issues')}")
    return result.data


def accept_sketch(worker: Any, spec: ContractCase, check_budget: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    before = read(worker, "model.observe", check_budget)
    actual = read(worker, "sketch.inspect", check_budget)
    after = read(worker, "model.observe", check_budget)
    return {**sketch_oracle(spec, actual, [before, after]), "readback": actual, "observations": [before, after]}, after


def accept_solid(worker: Any, spec: ContractCase, check_budget: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    # Same five-production-RPC pattern as the existing production smoke:
    # two independent observations on each side of native extreme-point reads.
    observations = [read(worker, "model.observe", check_budget), read(worker, "model.observe", check_budget)]
    measurement = read(worker, "model.measure_extents", check_budget)
    observations.extend([read(worker, "model.observe", check_budget), read(worker, "model.observe", check_budget)])
    return {**solid_oracle(spec, measurement, observations), "measurement": measurement, "observations": observations}, observations[-1]


def checked_step(worker: Any, raw_call: dict[str, Any], previous: dict[str, Any],
                 check_budget: Any, record: dict[str, Any]) -> dict[str, Any]:
    registry = build_tool_registry()
    call = registry.validate(ToolCall.from_mapping(raw_call))
    observed = read(worker, "model.observe", check_budget)
    if not stable_observations([previous, observed]):
        raise ContractFailure("Document/configuration/update marker changed between contract steps; no write submitted.")
    started = time.monotonic()
    check_budget(minimum_remaining=RPC_TIMEOUT_SECONDS + WRITE_FINISH_MARGIN_SECONDS)
    result = worker.call_checked(call, observed, RPC_TIMEOUT_SECONDS)
    check_budget()
    check = registry.postcondition(call.tool)
    if result.ok and check is not None:
        result.ok, reason = check(call, result.data, result.before, result.after)
        if not result.ok:
            result.error_code, result.message = "PostconditionFailed", reason
    receipt = {"call": call.to_dict(), "ok": result.ok, "message": result.message, "error_code": result.error_code,
               "elapsed_seconds": round(time.monotonic() - started, 3), "worker_duration_ms": result.duration_ms}
    # Never mistake `applied` for independent geometry evidence. Failure-only
    # execution/uncertainty details are useful and contain no live COM objects.
    if not result.ok:
        receipt["failure"] = {key: result.data[key] for key in ("execution", "mutation", "precondition") if key in result.data}
    record["steps"].append(receipt)
    if not result.ok:
        raise ContractFailure(f"{call.tool}: {result.error_code}: {result.message}")
    after = read(worker, "model.observe", check_budget)
    if any(after.get(key) != observed.get(key) for key in ("document_identity", "configuration", "configuration_identity")):
        raise ContractFailure("A contract step changed document/configuration identity.")
    return after


def save_reopen(worker: Any, spec: ContractCase, before: dict[str, Any],
                check_budget: Any, record: dict[str, Any]) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    layer: dict[str, Any] = {"status": "failed", "task_id": task_id, "automatic_run_called": False}
    record["save_reopen"] = layer
    check_budget(minimum_remaining=_CANDIDATE_TIMEOUTS["begin"] + WRITE_FINISH_MARGIN_SECONDS)
    begun = worker.candidate_request("begin", expected=before, task_id=task_id)
    check_budget()
    layer["begin_receipt"] = {key: begun.get(key) for key in ("ok", "status", "reason", "artifact_path")}
    if begun.get("ok") is not True or begun.get("status") != "begun" or not isinstance(begun.get("token"), str):
        raise ContractFailure(f"Save/reopen candidate begin failed: {begun.get('reason')}")
    copied, observed = accept_solid(worker, spec, check_budget)
    layer["copy_geometry"] = copied
    if copied["status"] != "passed" or observed.get("document_identity") == before.get("document_identity"):
        raise ContractFailure("Saved candidate is not an independently verified new document.")
    check_budget(minimum_remaining=_CANDIDATE_TIMEOUTS["finish"] + WRITE_FINISH_MARGIN_SECONDS)
    finished = worker.candidate_request("finish", token=begun["token"], accept=True, expected=observed)
    check_budget()
    layer["finish_receipt"] = {key: finished.get(key) for key in ("ok", "status", "reason", "checkpoint_id", "persisted_current_state")}
    checkpoint_id = finished.get("checkpoint_id")
    if (finished.get("ok") is not True or finished.get("status") != "committed"
            or finished.get("persisted_current_state") is not True or not isinstance(checkpoint_id, str)):
        raise ContractFailure(f"Native checkpoint save failed: {finished.get('reason')}")
    check_budget()
    original_signature = checkpoint_signature(worker.checkpoint_request("list", task_id=task_id), checkpoint_id)
    check_budget(minimum_remaining=_CHECKPOINT_TIMEOUTS["restore"] + WRITE_FINISH_MARGIN_SECONDS)
    restored = worker.checkpoint_request("restore", task_id=task_id, checkpoint_id=checkpoint_id)
    check_budget()
    layer["restore_receipt"] = {key: restored.get(key) for key in ("ok", "status", "reason", "checkpoint_id", "artifact_path", "checkpoint_preserved")}
    if restored.get("ok") is not True or restored.get("status") != "restored" or restored.get("checkpoint_preserved") is not True:
        raise ContractFailure(f"Native checkpoint reopen failed: {restored.get('reason')}")
    geometry, after = accept_solid(worker, spec, check_budget)
    layer["reopened_geometry"] = geometry
    check_budget()
    after_signature = checkpoint_signature(worker.checkpoint_request("list", task_id=task_id), checkpoint_id)
    check_budget()
    different_document = after.get("document_identity") not in {before.get("document_identity"), observed.get("document_identity")}
    layer.update(native_sha256=original_signature[0], native_size=original_signature[1],
                 immutable_checkpoint=after_signature == original_signature, independently_opened=different_document)
    if geometry["status"] != "passed" or not different_document or original_signature != after_signature:
        raise ContractFailure("Checkpoint re-open geometry, identity or native artifact integrity differs.")
    layer["status"] = "passed"
    return after


def execute_case(worker: Any, spec: ContractCase, check_budget: Any, record: dict[str, Any], *,
                 verify_save_reopen: bool, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if previous is not None:
        current = read(worker, "model.observe", check_budget)
        if not stable_observations([previous, current]):
            raise ContractFailure("The previous synthetic document changed before the next case.")
    check_budget()
    registry = build_tool_registry()
    call = registry.validate(ToolCall("document.new_part", {}))
    check_budget(minimum_remaining=RPC_TIMEOUT_SECONDS + WRITE_FINISH_MARGIN_SECONDS)
    created: StepResult = worker.call(call, RPC_TIMEOUT_SECONDS)  # App-scope creation, as in the production runner.
    check_budget()
    check = registry.postcondition(call.tool)
    valid = created.ok and (check is None or check(call, created.data, created.before, created.after)[0])
    record["steps"].append({"call": call.to_dict(), "ok": bool(valid), "message": created.message,
                            "error_code": created.error_code, "worker_duration_ms": created.duration_ms})
    if not valid:
        raise ContractFailure(f"Fresh synthetic part creation failed: {created.error_code}: {created.message}")
    current = read(worker, "model.observe", check_budget)
    snapshot = current.get("snapshot", {})
    if (not isinstance(current.get("document_identity"), str) or not current["document_identity"]
            or snapshot.get("document_type") != "part" or snapshot.get("document_path") != ""
            or snapshot.get("body_count") != 0 or snapshot.get("active_sketch") != ""
            or previous is not None and current["document_identity"] == previous.get("document_identity")):
        raise ContractFailure("Creation did not independently establish an empty, unsaved, new part.")
    record["document_identity"] = current["document_identity"]
    for raw in spec.sketch_calls:
        current = checked_step(worker, raw, current, check_budget, record)
    geometry, current = accept_sketch(worker, spec, check_budget)
    record["sketch_geometry"] = geometry
    if geometry["status"] != "passed":
        raise ContractFailure(f"Independent sketch oracle rejected geometry: {geometry.get('reason', geometry['checks'])}")
    current = checked_step(worker, spec.feature_call, current, check_budget, record)
    geometry, current = accept_solid(worker, spec, check_budget)
    record["solid_geometry"] = geometry
    if geometry["status"] != "passed":
        raise ContractFailure(f"Independent solid oracle rejected geometry: {geometry.get('reason', geometry['checks'])}")
    if verify_save_reopen:
        current = save_reopen(worker, spec, current, check_budget, record)
    record["status"] = "passed"
    return current


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    started = time.monotonic()
    source = source_fingerprint(ROOT)
    spec_digest = hashlib.sha256(json.dumps([item.public() for item in (case(cid, 1) for cid in CASE_IDS)], sort_keys=True).encode()).hexdigest()
    output = ROOT / "artifacts" / "cad-contract-matrix" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    if not output.resolve().is_relative_to(ROOT.resolve()):
        raise ToolError("Evidence path escapes the project workspace.")
    output.mkdir(parents=True, exist_ok=False)
    records = [{**case(case_id, repetition).public(), "selected": case_id in args.cases,
                "status": "not_run", "reason": "preflight_only" if not args.execute else "not_selected",
                "steps": [], "sketch_geometry": {"status": "not_run"}, "solid_geometry": {"status": "not_run"},
                "save_reopen": {"status": "not_run", "reason": "not_requested" if not args.verify_save_reopen else "not_executed"}}
               for repetition in range(1, args.repeat + 1) for case_id in CASE_IDS]
    report: dict[str, Any] = {
        "schema_version": 1, "status": "not_run", "passed": False, "mode": "execute" if args.execute else "preflight_only",
        "source": source, "environment": runtime_fingerprint(
            solidworks_version="not_observed", pi_version="not_used", model_id="fixed_local_contract_catalog",
            model_settings_sha256=spec_digest, cad_mode="real"),
        "cases": records, "selected_cases": args.cases, "repeats": args.repeat,
        "catalog_case_count": len(CASE_IDS), "coverage_scope": "full_catalog" if args.cases == list(CASE_IDS) else "selected_cases_only",
        "execution_path": "SolidWorksWorkerClient -> spawn -> worker_main -> checked_call -> SolidWorksGateway",
        "worker_attach_only": True, "synthetic_data_only": True, "external_model_requests": 0,
        "documents_closed_or_deleted": 0, "host_geometry_calls": 0, "budget_seconds": BUDGET_SECONDS,
        "rpc_timeout_seconds": RPC_TIMEOUT_SECONDS, "save_reopen_requested": args.verify_save_reopen,
        "write_budget_reservations_seconds": {
            "new_part_and_checked_call": RPC_TIMEOUT_SECONDS + WRITE_FINISH_MARGIN_SECONDS,
            "candidate_begin": _CANDIDATE_TIMEOUTS["begin"] + WRITE_FINISH_MARGIN_SECONDS,
            "candidate_finish": _CANDIDATE_TIMEOUTS["finish"] + WRITE_FINISH_MARGIN_SECONDS,
            "checkpoint_restore": _CHECKPOINT_TIMEOUTS["restore"] + WRITE_FINISH_MARGIN_SECONDS,
        },
        "start_solidworks_permitted": args.start_solidworks, "events": [], "preflight": {"status": "not_run"},
        "synchronous_host_header_reads": "Not forcibly interruptible; no CAD process is terminated.", "artifact_directory": str(output),
    }
    guard = MatrixBudget(started)
    gateway = worker = com = None
    initialized = False
    current_record = None

    def publish() -> None:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        temporary = output / "result.partial.json"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(output / "result.json")

    def progress(event: str, payload: dict[str, Any]) -> None:
        entry = {key: payload[key] for key in ("request_id", "tool", "operation", "phase", "elapsed_seconds", "timeout_seconds") if key in payload}
        entry.update(event=event, elapsed_run_seconds=round(time.monotonic() - started, 3))
        if current_record is not None:
            entry.update(case_id=current_record["case_id"], repetition=current_record["repetition"])
        if len(report["events"]) < 3000:
            report["events"].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    guard.timer.start()
    publish()
    try:
        import pythoncom
        from win32com.client import GetActiveObject

        com = pythoncom
        com.CoInitialize()
        initialized = True
        gateway = SolidWorksGateway(visible=True)
        with solidworks_serial_gate(30):
            guard.check()
            started_cad = attach_empty_session(gateway, allow_start=args.start_solidworks, get_active=GetActiveObject)
            revision = _read_member(gateway._sw, "RevisionNumber")
            if not isinstance(revision, str) or not revision:
                raise ToolError("Cannot read the actual SolidWorks revision.")
            report["preflight"] = {"status": "passed", "existing_document_count": 0,
                                   "solidworks_started_by_test": started_cad, "solidworks_version": revision}
        report["environment"] = runtime_fingerprint(solidworks_version=revision, pi_version="not_used", model_id="fixed_local_contract_catalog",
                                                     model_settings_sha256=spec_digest, cad_mode="real")
        guard.check()
        if args.execute:
            worker = SolidWorksWorkerClient(candidate_workspace=output / "candidates", on_progress=progress, allow_start_solidworks=False)
            guard.worker = worker
            expected_documents = 0
            previous = None
            # One outer cancellation epoch spans all cases and checkpoint reads.
            with worker.operation():
                for record in records:
                    if not record["selected"]:
                        continue
                    current_record = record
                    with solidworks_serial_gate(30):
                        guard.check()
                        if expected_documents == 0:
                            require_empty_session(gateway._sw)
                        elif document_count(gateway._sw) != expected_documents:
                            raise ContractFailure("Open-document count changed between cases; no next part was created.")
                    record["reason"] = "executing"
                    case_started = time.monotonic()
                    progress("case_started", {})
                    publish()
                    try:
                        previous = execute_case(worker, case(record["case_id"], record["repetition"]), guard.check, record,
                                                verify_save_reopen=args.verify_save_reopen, previous=previous)
                        expected_documents += 3 if args.verify_save_reopen else 1
                    finally:
                        record["duration_seconds"] = round(time.monotonic() - case_started, 3)
                        publish()
                    record.pop("reason", None)
                    progress("case_passed", {})
                with solidworks_serial_gate(30):
                    guard.check()
                    report["retained_document_count"] = document_count(gateway._sw)
                if report["retained_document_count"] != expected_documents:
                    raise ContractFailure("Final retained document count differs from the independently created case documents.")
            report["status"] = "passed"
            report["passed"] = all(item["status"] == "passed" for item in records if item["selected"])
    except BudgetBoundary as exc:
        # Stop at a known host boundary, before any next RPC submission. Cancel
        # the watchdog immediately so evidence/worker cleanup cannot turn this
        # refusal into an interrupt of the retained SolidWorks state.
        guard.timer.cancel()
        report.update(status="blocked", passed=False, reason_code="budget_boundary",
                      error_code=type(exc).__name__, reason=str(exc))
        if report["preflight"]["status"] != "passed":
            report["preflight"] = {"status": "blocked", "reason_code": "budget_boundary", "reason": str(exc)}
        if current_record is not None and current_record["status"] != "passed":
            current_record.update(status="blocked", reason_code="budget_boundary", reason=str(exc), error_code=type(exc).__name__)
            layer = current_record.get("save_reopen", {})
            if layer.get("status") == "failed":
                layer.update(status="blocked", reason_code="budget_boundary", reason=str(exc))
    except Exception as exc:
        report.update(status="blocked" if report["preflight"]["status"] != "passed" else "failed",
                      error_code=type(exc).__name__, reason=str(exc))
        if report["preflight"]["status"] != "passed":
            report["preflight"] = {"status": "blocked", "reason": str(exc)}
        if current_record is not None and current_record["status"] != "passed":
            current_record.update(status="failed", reason=str(exc), error_code=type(exc).__name__)
    finally:
        guard.timer.cancel()
        if worker is not None:
            worker.close()  # Stops helper only; never closes any CAD document.
        if gateway is not None:
            gateway.release(close_owned=False)
        if initialized and com is not None:
            com.CoUninitialize()
        for record in records:
            if record["selected"] and record["status"] == "not_run" and args.execute:
                record["reason"] = ("budget_boundary" if report.get("reason_code") == "budget_boundary" else
                                    "preflight_blocked" if report["preflight"]["status"] == "blocked" else "stopped_after_earlier_failure")
        report["budget_exhausted"] = guard.expired.is_set() or time.monotonic() >= guard.deadline
        if guard.expired.is_set() or (report["budget_exhausted"] and report.get("reason_code") != "budget_boundary"):
            report.update(status="failed", passed=False, reason="Total host budget exhausted; no replay performed.")
        report["source_tree_unchanged"] = source_fingerprint(ROOT)["source_tree_sha256"] == source["source_tree_sha256"]
        if not report["source_tree_unchanged"]:
            report.update(status="failed", passed=False, reason="Source tree changed during acceptance.")
        report["counts"] = {status: sum(item["status"] == status for item in records) for status in ("passed", "failed", "blocked", "not_run")}
        publish()
        print(json.dumps({"event": "matrix_finished", "status": report["status"], "passed": report["passed"],
                          "counts": report["counts"], "report": str(output / "result.json")}, ensure_ascii=False), flush=True)
    if report["status"] == "blocked":
        return 2
    return 0 if report["status"] in {"passed", "not_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
