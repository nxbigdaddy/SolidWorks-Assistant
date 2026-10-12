"""Offline-only safety and oracle tests; never starts COM, CAD, or a model."""
from __future__ import annotations

import ast
import copy
import inspect
import json
import math
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import live_production_reliability_smoke as smoke
from sw_assistant.domain import StepResult, ToolCall, ToolError
from sw_assistant.solidworks.client import SolidWorksWorkerClient
from sw_assistant.solidworks.worker import worker_main


class NotRunning(Exception):
    hresult = smoke.NOT_RUNNING_HRESULT


class Session:
    def __init__(self, count=0, documents=None, active=None):
        self.GetDocumentCount = count
        self.GetDocuments = documents
        self.ActiveDoc = active


class Gateway:
    def __init__(self, session=None, visible=True):
        self._sw = None
        self.session = session or Session()
        self._owns_application = False
        self.starts = 0
        self.releases = []

    def connect(self):
        self.starts += 1
        self._sw = self.session
        self._owns_application = True

    def release(self, *, close_owned=False):
        self.releases.append(close_owned)


def absent(_name):
    raise NotRunning()


@pytest.mark.parametrize("args", [[], ["--start-solidworks"], ["--execute", "--budget-seconds", "3600"],
                                  ["--execute", "--output", "C:/user-data"]])
def test_cli_requires_opt_in_and_has_no_path_or_budget_override(monkeypatch, tmp_path, args):
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "SolidWorksGateway", lambda **_: pytest.fail("COM must not be attached"))
    with pytest.raises(SystemExit) as raised:
        smoke.main(args)
    assert raised.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_live_script_imports_real_production_classes_and_has_no_worker_override():
    assert smoke.SolidWorksWorkerClient is SolidWorksWorkerClient
    assert smoke.ContinuousTaskRunner.__module__ == "sw_assistant.agent.task_runner"
    assert smoke.FixtureBackend.__module__ == "scripts.live_candidate_trial_smoke"
    tree = ast.parse(inspect.getsource(smoke))
    assert not any(isinstance(item, ast.ClassDef) and "Worker" in item.name for item in ast.walk(tree))
    assert not any(isinstance(item, ast.Name) and item.id == "InlineWorker" for item in ast.walk(tree))
    modeling_calls = [item for item in ast.walk(tree) if isinstance(item, ast.Call)
                      and isinstance(item.func, ast.Attribute) and isinstance(item.func.value, ast.Name)
                      and item.func.value.id == "gateway"]
    assert {item.func.attr for item in modeling_calls} <= {"connect", "release"}
    client = SolidWorksWorkerClient(candidate_workspace=Path.cwd() / "artifacts" / "never-started")
    assert client._context.get_start_method() == "spawn"
    assert not client.alive
    assert worker_main.__module__ == "sw_assistant.solidworks.worker"


@pytest.mark.parametrize("session", [Session(1, [object()]), Session(0, [], object()),
                                     Session(2, [object(), object()])])
@pytest.mark.parametrize("allow_start", [False, True])
def test_existing_any_document_refused_even_if_inactive_or_launch_allowed(session, allow_start):
    gateway = Gateway()
    with pytest.raises(ToolError, match="open documents"):
        smoke.attach_empty_session(gateway, allow_start=allow_start, get_active=lambda _: session)
    assert gateway.starts == 0


@pytest.mark.parametrize("count,documents", [(None, None), (True, []), (-1, []), (0.0, []),
                                             ("0", []), (0, {}), (0, ""), (1, None), (0, [object()])])
def test_unknown_or_racing_document_enumeration_is_not_treated_as_empty(count, documents):
    with pytest.raises(ToolError):
        smoke.require_empty_session(Session(count, documents))


def test_not_running_without_launch_flag_never_connects():
    gateway = Gateway()
    with pytest.raises(ToolError, match="--start-solidworks"):
        smoke.attach_empty_session(gateway, allow_start=False, get_active=absent)
    assert gateway.starts == 0


def test_explicit_launch_only_for_missing_rot_session():
    gateway = Gateway()
    assert smoke.attach_empty_session(gateway, allow_start=True, get_active=absent)
    assert gateway.starts == 1


def test_arbitrary_com_error_never_triggers_launch():
    gateway = Gateway()

    def broken(_name):
        raise RuntimeError("private document name must not reach preflight message")

    with pytest.raises(ToolError, match="no launch") as raised:
        smoke.attach_empty_session(gateway, allow_start=True, get_active=broken)
    assert gateway.starts == 0
    assert "private" not in str(raised.value)


def test_launch_that_auto_restores_existing_documents_is_refused():
    gateway = Gateway(Session(1, [object()]))
    with pytest.raises(ToolError, match="open documents"):
        smoke.attach_empty_session(gateway, allow_start=True, get_active=absent)
    assert gateway.starts == 1


def evidence():
    identity = {"document_identity": "fixture", "configuration": "Default", "configuration_identity": "config"}
    observation = {**identity, "complete": True, "model_update_stamp": 71, "snapshot": {
        "volume_m3": (693 - 5 * math.pi) / 1e9, "body_count": 1, "active_sketch": "",
    }, "features": [{"name": "boss", "error_code": 0}, {"name": "cut", "error_code": 0}]}
    measurement = {**identity, "complete": True, "issues": [], "source": "IBody2.GetExtremePoint",
                   "coordinate_system": "model", "bodies_count": 1, "extents_mm": {"x": 7, "y": 9, "z": 11}}
    return [StepResult(True, ToolCall("model.measure_extents" if i == 2 else "model.observe", {}), "",
                       data=copy.deepcopy(measurement if i == 2 else observation)) for i in range(5)]


class ReadWorker:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def call(self, call, timeout_seconds=120):
        self.calls.append((call, timeout_seconds))
        return self.results[len(self.calls) - 1]


def test_independent_oracle_uses_five_production_rpcs_and_four_distinct_update_reads():
    worker = ReadWorker(evidence())
    checks = []
    result = smoke.production_acceptance(worker, lambda: checks.append(True))
    assert result["passed"]
    assert result["production_rpc"] and result["observation_count"] == 4
    assert result["update_stamps"] == [71] * 4
    assert [call.tool for call, _ in worker.calls] == [
        "model.observe", "model.observe", "model.measure_extents", "model.observe", "model.observe",
    ]
    assert all(timeout == 120 for _, timeout in worker.calls)
    assert all(call.arguments == {"include_configuration": True} for call, _ in worker.calls if call.tool == "model.observe")
    assert len(checks) == 10
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("index", [0, 1, 3, 4])
@pytest.mark.parametrize("stamp", [None, True, -1, 71.0, "71", math.nan])
def test_independent_unknown_stamp_fails_closed(index, stamp):
    results = evidence()
    results[index].data["model_update_stamp"] = stamp
    with pytest.raises(ToolError, match="marker"):
        smoke.production_acceptance(ReadWorker(results), lambda: None)


@pytest.mark.parametrize("index", [0, 1, 3, 4])
def test_changed_same_volume_model_or_configuration_fails(index):
    results = evidence()
    results[index].data["model_update_stamp"] += 1
    result = smoke.production_acceptance(ReadWorker(results), lambda: None)
    assert not result["passed"]
    assert not result["checks"]["stable_update_stamp"]


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_failed_rpc_cannot_pass_independent_acceptance(index):
    results = evidence()
    results[index].ok = False
    if index == 2:
        assert not smoke.production_acceptance(ReadWorker(results), lambda: None)["passed"]
    else:
        with pytest.raises(ToolError, match="failed or incomplete"):
            smoke.production_acceptance(ReadWorker(results), lambda: None)


@pytest.mark.parametrize("index", [0, 1, 3, 4])
@pytest.mark.parametrize("field,value", [("body_count", 2), ("volume_m3", 7e-9), ("active_sketch", "UnexpectedSketch")])
def test_every_observation_rechecks_geometry_not_only_stamps(index, field, value):
    results = evidence()
    results[index].data["snapshot"][field] = value
    assert not smoke.production_acceptance(ReadWorker(results), lambda: None)["passed"]


def test_expired_budget_never_calls_next_rpc():
    worker = ReadWorker(evidence())
    guard = smoke.BudgetGuard(time.monotonic() - 1801)
    with pytest.raises(ToolError, match="1800-second"):
        smoke.production_acceptance(worker, guard.check)
    assert guard.expired.is_set()
    assert worker.calls == []


def test_watchdog_uses_runner_stop_not_direct_solidworks_termination():
    stops = []
    guard = smoke.BudgetGuard(time.monotonic())
    guard.worker = SimpleNamespace(interrupt=lambda: pytest.fail("runner owns stop ordering"))
    guard.runner = SimpleNamespace(stop=lambda: stops.append(True))
    guard.cancel()
    assert stops == [True] and guard.expired.is_set()


CHECKPOINT_ID = "a" * 32


def checkpoint_catalog(digest="b" * 64, size=123):
    return {"ok": True, "status": "listed", "issues": [], "checkpoints": [{
        "checkpoint_id": CHECKPOINT_ID, "integrity": "verified", "native_sha256": digest, "native_size": size,
    }]}


class RestoreRunner:
    def __init__(self, *, status="ready"):
        self.restores = []
        self.restore_status = status
        self.state = {"id": "task", "status": "completed", "active_checkpoint_id": CHECKPOINT_ID,
                      "checkpoint_records": [{"task_id": "task", "checkpoint_id": CHECKPOINT_ID}],
                      "goals": [{"id": "fixed_goal"}], "requirements": [{"id": "fixed_requirement"}],
                      "actions": 10, "rounds": 5}

    def snapshot(self):
        return copy.deepcopy(self.state)

    def restore_checkpoint(self, checkpoint_id):
        self.restores.append(checkpoint_id)
        self.state.update(status=self.restore_status, pending_steps=[], pending_decision=None)
        return self.snapshot()

    def run(self):
        pytest.fail("Restored task must never automatically run")


class RestoreWorker(ReadWorker):
    def __init__(self, after_digest="b" * 64):
        super().__init__(evidence())
        self.catalogs = [checkpoint_catalog(), checkpoint_catalog(after_digest)]
        self.lists = []
        self.scopes = 0

    def operation(self):
        self.scopes += 1
        return nullcontext()

    def checkpoint_request(self, operation, *, task_id):
        self.lists.append((operation, task_id))
        return self.catalogs.pop(0)


def test_restore_acceptance_uses_host_latest_checkpoint_ready_boundary_and_unchanged_native_hash():
    runner, worker = RestoreRunner(), RestoreWorker()
    result = smoke.verify_checkpoint_restore(runner, worker, lambda: None)
    assert result["passed"] and result["status"] == "ready"
    assert result["automatic_run_called"] is False
    assert runner.restores == [CHECKPOINT_ID] and worker.scopes == 1
    assert worker.lists == [("list", "task"), ("list", "task")]
    assert len(worker.calls) == 5
    assert result["checks"]["immutable_checkpoint"]
    assert result["native_sha256_before"] == result["native_sha256_after"] == "b" * 64
    assert result["independent_acceptance"]["passed"]


def test_restore_acceptance_rejects_checkpoint_mutation_even_if_geometry_matches():
    result = smoke.verify_checkpoint_restore(RestoreRunner(), RestoreWorker("c" * 64), lambda: None)
    assert not result["passed"] and not result["checks"]["immutable_checkpoint"]
    assert result["independent_acceptance"]["passed"]


@pytest.mark.parametrize("status", ["review", "running", "completed", "stopped", "failed"])
def test_restore_unknown_or_non_ready_result_does_not_trigger_more_cad_reads(status):
    worker = RestoreWorker()
    result = smoke.verify_checkpoint_restore(RestoreRunner(status=status), worker, lambda: None)
    assert not result["passed"] and len(worker.lists) == 1 and worker.calls == []


@pytest.mark.parametrize("change", [{"checkpoint_records": []}, {"status": "ready"},
                                  {"active_checkpoint_id": "c" * 32}, {"id": "other-task"}])
def test_restore_requires_latest_matching_completed_host_record(change):
    runner, worker = RestoreRunner(), RestoreWorker()
    runner.state.update(change)
    with pytest.raises(ToolError, match="host-bound"):
        smoke.verify_checkpoint_restore(runner, worker, lambda: None)
    assert worker.calls == worker.lists == runner.restores == []


@pytest.mark.parametrize("value", [None, "", "a" * 63, "A" * 64, "../path", True])
def test_restore_digest_must_be_native_verified_sha256(value):
    with pytest.raises(ToolError):
        smoke.checkpoint_signature(checkpoint_catalog(value), CHECKPOINT_ID)


@pytest.mark.parametrize("value", [None, 0, -1, True, 123.0, "123"])
def test_restore_native_size_must_be_positive_integer(value):
    with pytest.raises(ToolError):
        smoke.checkpoint_signature(checkpoint_catalog(size=value), CHECKPOINT_ID)


@pytest.mark.parametrize("open_count", [4, 5])
@pytest.mark.parametrize("status", ["completed", "review"])
def test_main_keeps_production_constructor_operation_epoch_progress_and_all_documents(monkeypatch, tmp_path, open_count, status):
    gateway = Gateway()
    calls = []
    instances = []

    class Client(ReadWorker):
        def __init__(self, *, candidate_workspace, on_progress, allow_start_solidworks):
            assert allow_start_solidworks is False
            super().__init__(evidence())
            self.workspace = candidate_workspace
            self.closed = False
            self.in_operation = False
            instances.append(self)
            on_progress("rpc.started", {"tool": "model.observe", "phase": "dispatch",
                                        "arguments": {"secret": "not allowed"}, "document_title": "not allowed"})

        def operation(self):
            self.in_operation = True
            calls.append("operation")
            return nullcontext()

        def close(self):
            self.closed = True

    result = {"id": "fixed-test", "status": status, "rounds": 5,
              "trial_history": [{"status": item} for item in smoke.EXPECTED_TRIALS]}
    native_count_reads = []
    native_document_count = smoke.document_count

    def count_documents(application):
        native_count_reads.append(True)
        return native_document_count(application)

    class Runner:
        def __init__(self, registry, worker, backend, store, **kwargs):
            assert type(worker) is Client and kwargs["max_rounds"] == 6
            self.backend = backend
            self.worker = worker

        def prepare(self, prompt):
            assert self.worker.in_operation
            calls.append("prepare")
            return {"status": "ready"}

        def run(self):
            calls.append("run")
            self.backend.rounds = 5
            gateway._sw.GetDocumentCount = open_count
            gateway._sw.GetDocuments = [object()] * open_count
            return result

        def snapshot(self):
            return result

    pythoncom = SimpleNamespace(CoInitialize=lambda: calls.append("co-init"), CoUninitialize=lambda: calls.append("co-uninit"))
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com.client", SimpleNamespace(GetActiveObject=lambda _: gateway.session))
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "SolidWorksGateway", lambda **kwargs: gateway)
    monkeypatch.setattr(smoke, "SolidWorksWorkerClient", Client)
    monkeypatch.setattr(smoke, "ContinuousTaskRunner", Runner)
    monkeypatch.setattr(smoke, "solidworks_serial_gate", lambda _timeout: nullcontext())
    monkeypatch.setattr(smoke, "document_count", count_documents)
    passed = open_count == 4 and status == "completed"
    assert smoke.main(["--execute"]) == (0 if passed else 1)
    reports = list(tmp_path.rglob("result.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["passed"] is passed
    if status == "completed":
        assert report["retained_open_document_count"] == open_count
        assert len(native_count_reads) == 3
    else:
        assert "retained_open_document_count" not in report
        assert "independent_acceptance" not in report
        assert instances[0].calls == []
        assert len(native_count_reads) == 2  # No native probe after uncertain execution.
    assert report["backend"] == "fixed_local_fixture_not_pi"
    assert report["external_model_requests"] == 0 and report["host_geometry_calls"] == 0
    assert report["startup_existing_document_count"] == 0
    assert "not allowed" not in json.dumps(report)
    assert report["rpc_events"][0]["event"] == "rpc.started"
    assert instances[0].closed and instances[0].workspace.is_relative_to(tmp_path)
    assert gateway.releases == [False] and gateway.starts == 0
    assert calls == ["co-init", "operation", "prepare", "run", "co-uninit"]
