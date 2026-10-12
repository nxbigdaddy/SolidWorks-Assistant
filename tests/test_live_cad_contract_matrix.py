"""Offline entry-point and RPC-boundary tests; never connects to SolidWorks."""
from __future__ import annotations

import ast
import inspect
import json
from contextlib import nullcontext

import pytest

from scripts import live_cad_contract_matrix as matrix
from scripts.cad_contract_catalog import CASE_IDS, case
from test_production_reliability_script import Gateway, Session, absent
from sw_assistant.domain import StepResult
from sw_assistant.solidworks.client import SolidWorksWorkerClient


def environment(monkeypatch, tmp_path, *, documents=0, not_running=False, failure_at=0, changed_source=False):
    import pythoncom
    import win32com.client

    session = Session(documents, [object() for _ in range(documents)])
    session.RevisionNumber = "33.0.1"
    gateway = Gateway(session)
    source_calls = []

    def fingerprint(_root):
        source_calls.append(True)
        return {"source_tree_sha256": ("b" if changed_source and len(source_calls) > 1 else "a") * 64}

    monkeypatch.setattr(matrix, "ROOT", tmp_path)
    monkeypatch.setattr(matrix, "source_fingerprint", fingerprint)
    monkeypatch.setattr(matrix, "runtime_fingerprint", lambda **values: values)
    monkeypatch.setattr(matrix, "SolidWorksGateway", lambda **_: gateway)
    monkeypatch.setattr(matrix, "solidworks_serial_gate", lambda _: nullcontext())
    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: None)
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: None)
    monkeypatch.setattr(win32com.client, "GetActiveObject", absent if not_running else lambda _: session)
    workers = []

    class Worker:
        def __init__(self, **options):
            self.options, self.closed, self.interrupted = options, False, False
            workers.append(self)

        def operation(self):
            return nullcontext()

        def close(self):
            self.closed = True

        def interrupt(self):
            self.interrupted = True

    monkeypatch.setattr(matrix, "SolidWorksWorkerClient", Worker)
    executions = []

    def execute(_worker, spec, check, record, *, verify_save_reopen, previous):
        check()
        executions.append(spec.case_id)
        if len(executions) == failure_at:
            raise matrix.ContractFailure("synthetic geometry mismatch")
        session.GetDocumentCount += 3 if verify_save_reopen else 1
        session.GetDocuments = [object() for _ in range(session.GetDocumentCount)]
        session.ActiveDoc = session.GetDocuments[-1]
        record["status"] = "passed"
        return {"complete": True, "document_identity": f"synthetic-{len(executions)}"}

    monkeypatch.setattr(matrix, "execute_case", execute)
    return gateway, workers, executions


def report(tmp_path):
    paths = list(tmp_path.rglob("result.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("args", [[], ["--preflight-only"]])
def test_default_preflight_never_starts_worker_or_writes_cad(monkeypatch, tmp_path, args):
    gateway, workers, executions = environment(monkeypatch, tmp_path)
    assert matrix.main(args) == 0
    actual = report(tmp_path)
    assert actual["status"] == "not_run" and actual["preflight"]["status"] == "passed"
    assert not actual["passed"] and actual["counts"]["not_run"] == len(CASE_IDS)
    assert not workers and not executions and gateway.starts == 0
    assert gateway.releases == [False]


@pytest.mark.parametrize("args", [["--start-solidworks"], ["--verify-save-reopen"], ["--execute", "--preflight-only"],
                                  ["--execute", "--repeat", "4"], ["--case", "other"], ["--execute", "--output", "C:/user"],
                                  ["--execute", "--case", "all", "--case", "front.circle"]])
def test_unsupported_cli_options_do_nothing(monkeypatch, tmp_path, args):
    monkeypatch.setattr(matrix, "ROOT", tmp_path)
    monkeypatch.setattr(matrix, "source_fingerprint", lambda _: pytest.fail("must reject before source/COM access"))
    with pytest.raises(SystemExit) as raised:
        matrix.main(args)
    assert raised.value.code == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("flags", [["--execute"], ["--execute", "--start-solidworks"], []])
def test_any_existing_document_blocks_every_cad_write(monkeypatch, tmp_path, flags):
    gateway, workers, executions = environment(monkeypatch, tmp_path, documents=1)
    assert matrix.main(flags) == 2
    actual = report(tmp_path)
    assert actual["status"] == "blocked" and not actual["passed"]
    assert not workers and not executions and gateway.starts == 0
    assert actual["counts"]["not_run"] == len(CASE_IDS)


def test_absent_solidworks_without_flag_cannot_launch(monkeypatch, tmp_path):
    gateway, workers, _ = environment(monkeypatch, tmp_path, not_running=True)
    assert matrix.main(["--execute"]) == 2
    assert gateway.starts == 0 and workers == []


def test_explicit_host_launch_still_keeps_spawned_worker_attach_only(monkeypatch, tmp_path):
    gateway, workers, executions = environment(monkeypatch, tmp_path, not_running=True)
    assert matrix.main(["--execute", "--start-solidworks"]) == 0
    assert gateway.starts == 1 and len(workers) == 1
    assert workers[0].options["allow_start_solidworks"] is False and workers[0].closed
    assert executions == ["front.rectangle"]
    actual = report(tmp_path)
    assert actual["passed"] and actual["counts"]["passed"] == 1
    assert actual["counts"]["not_run"] == len(CASE_IDS) - 1
    assert actual["coverage_scope"] == "selected_cases_only"
    assert actual["retained_document_count"] == 1


def test_selected_all_case_repeat_and_save_document_count_is_bounded(monkeypatch, tmp_path):
    _, _, executions = environment(monkeypatch, tmp_path)
    assert matrix.main(["--execute", "--case", "all", "--repeat", "2", "--verify-save-reopen"]) == 0
    actual = report(tmp_path)
    assert len(executions) == len(CASE_IDS) * 2
    assert actual["counts"]["passed"] == len(CASE_IDS) * 2
    assert actual["retained_document_count"] == len(CASE_IDS) * 2 * 3


def test_first_failure_stops_following_cases_without_cleanup_or_fallback(monkeypatch, tmp_path):
    gateway, workers, executions = environment(monkeypatch, tmp_path, failure_at=2)
    assert matrix.main(["--execute", "--case", "all"]) == 1
    actual = report(tmp_path)
    assert executions == list(CASE_IDS[:2])
    assert actual["counts"] == {"passed": 1, "failed": 1, "blocked": 0, "not_run": len(CASE_IDS) - 2}
    assert "retained_document_count" not in actual  # No COM read after uncertain execution.
    assert gateway.releases == [False] and workers[0].closed


def test_changed_source_fingerprint_cannot_pass(monkeypatch, tmp_path):
    environment(monkeypatch, tmp_path, changed_source=True)
    assert matrix.main(["--execute"]) == 1
    actual = report(tmp_path)
    assert actual["source_tree_unchanged"] is False and actual["passed"] is False


def test_script_reuses_real_spawn_client_and_contains_no_direct_com_modeling():
    assert matrix.SolidWorksWorkerClient is SolidWorksWorkerClient
    tree = ast.parse(inspect.getsource(matrix))
    assert not any(isinstance(node, ast.ClassDef) and "Worker" in node.name for node in ast.walk(tree))
    forbidden = {"CloseDoc", "EditUndo2", "EditDelete", "NewPart", "NewDocument", "Save3", "OpenDoc6", "Dispatch"}
    assert not any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree))
    assert "applied" not in inspect.getsource(matrix.accept_solid)
    assert "applied" not in inspect.getsource(matrix.accept_sketch)


def test_independent_acceptance_calls_real_read_rpc_sequence_only():
    calls = []

    class Worker:
        def call(self, call, timeout):
            calls.append((call.tool, timeout))
            return StepResult(True, call, "synthetic", data={"complete": True, "issues": []})

    result, _ = matrix.accept_solid(Worker(), case("front.rectangle"), lambda **_kwargs: None)
    assert result["status"] == "failed"  # Empty success receipts prove nothing.
    assert [tool for tool, _ in calls] == ["model.observe", "model.observe", "model.measure_extents", "model.observe", "model.observe"]
    assert all(timeout == 120 for _, timeout in calls)


def test_checked_step_rejects_document_drift_before_submit():
    calls = []

    class Worker:
        def call(self, call, timeout):
            calls.append(call.tool)
            return StepResult(True, call, "", data={"complete": True, "issues": [], "document_identity": "changed"})

        def call_checked(self, *_args):
            pytest.fail("state drift must block before a write")

    with pytest.raises(matrix.ContractFailure, match="between contract steps"):
        matrix.checked_step(Worker(), case("front.rectangle").sketch_calls[0], {}, lambda **_kwargs: None, {"steps": []})
    assert calls == ["model.observe"]


def test_read_rpc_failure_or_incomplete_result_stops_without_replay():
    calls = []

    class Worker:
        def call(self, call, timeout):
            calls.append(call.tool)
            return StepResult(False, call, "uncertain timeout", error_code="WorkerError", data={"complete": True})

    with pytest.raises(matrix.ContractFailure, match="RPC failed"):
        matrix.accept_solid(Worker(), case("front.circle"), lambda **_kwargs: None)
    assert calls == ["model.observe"]


def checkpoint_fixture(monkeypatch, *, failed_finish=False, changed_digest=False, same_document=False):
    reads = iter([("copy", "passed"), ("source" if same_document else "reopened", "passed")])
    monkeypatch.setattr(matrix, "accept_solid", lambda *_args: (
        lambda pair: ({"status": pair[1]}, {"document_identity": pair[0]}))(next(reads)))
    events = []

    class Worker:
        lists = 0

        def candidate_request(self, operation, **payload):
            events.append(operation)
            if operation == "begin":
                return {"ok": True, "status": "begun", "token": "bounded-token"}
            return {"ok": not failed_finish, "status": "committed" if not failed_finish else "unavailable",
                    "checkpoint_id": "a" * 32, "persisted_current_state": True, "reason": "synthetic failure"}

        def checkpoint_request(self, operation, **payload):
            events.append(operation)
            if operation == "restore":
                return {"ok": True, "status": "restored", "checkpoint_preserved": True}
            self.lists += 1
            digest = "c" if changed_digest and self.lists == 2 else "b"
            return {"ok": True, "status": "listed", "issues": [], "checkpoints": [{"checkpoint_id": "a" * 32,
                      "integrity": "verified", "native_sha256": digest * 64, "native_size": 123}]}

    return Worker(), events


def test_save_reopen_uses_host_checkpoints_and_independent_new_document_readback(monkeypatch):
    worker, events = checkpoint_fixture(monkeypatch)
    record = {}
    after = matrix.save_reopen(worker, case("top.circle"), {"document_identity": "source"}, lambda **_kwargs: None, record)
    assert after["document_identity"] == "reopened"
    assert record["save_reopen"]["status"] == "passed"
    assert events == ["begin", "finish", "list", "restore", "list"]


@pytest.mark.parametrize("options", [{"failed_finish": True}, {"changed_digest": True}, {"same_document": True}])
def test_save_reopen_failure_cannot_become_a_pass_or_trigger_repeat(monkeypatch, options):
    worker, events = checkpoint_fixture(monkeypatch, **options)
    record = {}
    with pytest.raises(matrix.ContractFailure):
        matrix.save_reopen(worker, case("top.revolve"), {"document_identity": "source"}, lambda **_kwargs: None, record)
    assert record["save_reopen"]["status"] == "failed"
    assert events.count("begin") == events.count("finish") == 1
    if options.get("failed_finish"):
        assert events == ["begin", "finish"]
