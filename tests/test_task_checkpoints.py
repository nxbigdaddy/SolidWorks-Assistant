from __future__ import annotations

import copy

import pytest

from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import StepResult
from sw_assistant.tools import build_tool_registry
from test_continuous_task_runner import Backend, Worker, act, call
from test_gateway_checkpoints import Document, Gateway, observation

KEY = "a" * 32


class RecoveryWorker(Worker):
    def __init__(self):
        super().__init__()
        self.native = observation(Gateway(Document()))
        self.rpc = []
        self.on_rpc = None
        self.reject = False
        self.mismatch = False
        self.bad_fresh = False

    def checkpoint_request(self, operation, **payload):
        self.rpc.append((operation, payload))
        if self.on_rpc:
            self.on_rpc(operation)
        if operation == "list":
            return {"ok": True, "status": "listed", "issues": [], "checkpoints": [
                {"checkpoint_id": KEY, "parent_id": None, "integrity": "verified", "created_at": "2026-09-08"}]}
        if self.reject:
            return {"ok": False, "status": "unavailable", "reason": "unknown active document"}
        self.native["document_identity"] = "restored-copy"
        self.native["configuration_identity"] = "restored-config"
        self.native["snapshot"].update(document_title="restored", document_path="C:/owned/restored.sldprt")
        if self.mismatch:
            self.native["snapshot"]["volume_m3"] *= 2
        return {"ok": True, "status": "restored", "checkpoint_id": KEY,
                "checkpoint_preserved": True, "observation": copy.deepcopy(self.native)}

    def call(self, command, timeout_seconds=120):
        if command.tool == "model.observe":
            self.calls.append(command.tool)
            result = copy.deepcopy(self.native)
            if self.bad_fresh:
                result["snapshot"]["volume_m3"] *= 2
            return StepResult(True, command, "observed", data=result)
        return super().call(command, timeout_seconds)


def host_task(tmp_path):
    worker = RecoveryWorker()
    backend = Backend([act([call("session.ensure"), call("document.new_part")])])
    runner = ContinuousTaskRunner(build_tool_registry(), worker, backend, TaskStore(tmp_path / "tasks"))
    runner.prepare("保留全部原始结构与尺寸要求")
    runner._task.update(completed_steps=[{"tool": "session.ensure"}, {"tool": "document.new_part"}],
                        created_features=[{"name": "Boss1", "type": "Boss"}])
    assert runner._record_checkpoint({"persisted_current_state": True, "checkpoint_id": KEY}, worker.native, runner._epoch)
    return runner, worker, backend


def test_host_restore_preserves_user_contract_and_budget_without_running_model(tmp_path):
    runner, worker, backend = host_task(tmp_path)
    runner._task["completed_steps"].append({"tool": "feature.extrude", "arguments": {"depth_mm": 99}})
    runner._task["created_features"].append({"name": "LaterFeature", "type": "Boss"})
    runner._task.update(actions=48, rounds=7, no_progress=5, failure_attempts=[{"reason": "old failure"}],
                        verification={"passed": True}, created_circles=[{"id": "old-ref"}],
                        pending_messages=[{"content": "保留新补充要求"}], active_trial={"token": "old-token", "status": "running"})
    before = runner.snapshot()
    restored = runner.restore_checkpoint(KEY)
    assert restored["status"] == "ready", restored["message"]
    assert restored["completed_steps"] == before["checkpoint_records"][0]["prefix"]["completed_steps"]
    for field in ("goals", "requirements", "messages", "pending_messages", "actions", "rounds", "no_progress", "failure_attempts"):
        assert restored[field] == before[field]
    assert restored["active_trial"] is None
    assert restored["trial_history"][-1]["status"] == "abandoned"
    assert "token" not in restored["trial_history"][-1]
    assert restored["verification"] == {} and restored["created_circles"] == []
    assert restored["gateway_context_reset"] is True
    assert restored["latest_observation"]["document_identity"] == "restored-copy"
    assert worker.calls == ["session.ensure", "model.observe"]
    assert worker.rpc == [("restore", {"task_id": before["id"], "checkpoint_id": KEY})]
    assert len(backend.contexts) == 1


def test_checkpoint_id_survives_saved_record_and_new_runner(tmp_path):
    runner, worker, _ = host_task(tmp_path)
    task_id = runner.snapshot()["id"]
    saved = runner.store.load(task_id)
    assert saved["checkpoint_records"][0]["checkpoint_id"] == KEY
    new = ContinuousTaskRunner(build_tool_registry(), worker, Backend([]), runner.store)
    new.restore(task_id)
    assert new.restore_checkpoint(KEY)["status"] == "ready"


@pytest.mark.parametrize("broken", ["task", "id", "duplicate", "prefix", "extra_goals", "observation"])
def test_host_record_mismatch_never_calls_native_restore(tmp_path, broken):
    runner, worker, _ = host_task(tmp_path)
    record = runner._task["checkpoint_records"][0]
    if broken == "task":
        record["task_id"] = "other"
    elif broken == "id":
        record["checkpoint_id"] = "token-old"
    elif broken == "duplicate":
        runner._task["checkpoint_records"].append(copy.deepcopy(record))
    elif broken == "prefix":
        record["prefix"]["completed_steps"] = []
    elif broken == "extra_goals":
        record["prefix"]["goals"] = []
    else:
        record["observation"]["complete"] = False
    assert runner.restore_checkpoint(KEY)["status"] == "review"
    assert worker.calls == [] and worker.rpc == []


@pytest.mark.parametrize("failure", ["reject", "mismatch", "bad_fresh"])
def test_unverified_restore_never_rewinds_success_prefix(tmp_path, failure):
    runner, worker, _ = host_task(tmp_path)
    runner._task["completed_steps"].append({"tool": "feature.extrude"})
    before = runner.snapshot()["completed_steps"]
    setattr(worker, failure, True)
    result = runner.restore_checkpoint(KEY)
    assert result["status"] == "review"
    assert result["completed_steps"] == before
    assert len(worker.rpc) == 1


def test_stop_during_restore_ignores_late_response_and_does_not_read_or_resume(tmp_path):
    runner, worker, _ = host_task(tmp_path)
    worker.on_rpc = lambda _: runner.stop()
    result = runner.restore_checkpoint(KEY)
    assert result["status"] == "stopped"
    assert worker.calls == ["session.ensure"]
    assert "checkpoint_restore_history" not in result


def test_preflight_denial_and_prior_stop_never_issue_restore(tmp_path):
    runner, worker, _ = host_task(tmp_path)
    runner.preflight = lambda _: (False, "只读模式")
    assert runner.restore_checkpoint(KEY)["status"] == "review"
    assert worker.calls == []
    runner.stop()
    assert runner.restore_checkpoint(KEY)["status"] == "stopped"
    assert worker.calls == [] and worker.rpc == []


def test_list_requires_matching_host_prefix_and_is_readonly(tmp_path):
    runner, worker, _ = host_task(tmp_path)
    listed = runner.refresh_checkpoints()
    assert listed["checkpoint_catalog"][0]["host_prefix_available"] is True
    assert listed["checkpoint_catalog"][0]["completed_count"] == 2
    runner._task["checkpoint_records"][0]["task_id"] = "other"
    listed = runner.refresh_checkpoints()
    assert listed["checkpoint_catalog"][0]["host_prefix_available"] is False
    assert worker.calls == []


@pytest.mark.parametrize("bad", [None, "token-1", "../escape", "", True])
def test_missing_durable_receipt_does_not_claim_checkpoint(tmp_path, bad):
    runner, _, _ = host_task(tmp_path)
    previous = copy.deepcopy(runner._task["checkpoint_records"])
    assert not runner._record_checkpoint({"persisted_current_state": True, "checkpoint_id": bad}, {}, runner._epoch)
    assert runner.snapshot()["checkpoint_records"] == previous
