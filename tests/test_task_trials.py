"""Failure injection on arbitrary synthetic parts, no CAD or network access."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from sw_assistant.agent.task_models import TaskDecision, TrialSpec
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import StepResult
from sw_assistant.tools import build_tool_registry
from test_continuous_task_runner import Backend, Worker, act, call, complete, goal


class CandidateWorker(Worker):
    candidate_workspace_enabled = True

    def __init__(self):
        super().__init__()
        self.identity = "source"
        self.rpc = []
        self.source = None
        self.number = 0
        self.on_rpc = None
        self.restore_ok = True

    def observe(self):
        from sw_assistant.domain import ToolCall
        return self.call(ToolCall("model.observe", {})).data

    def call(self, command, timeout_seconds=120):
        result = super().call(command, timeout_seconds)
        if command.tool == "model.observe":
            result.data.update(document_identity=self.identity, configuration="Default",
                               configuration_identity="config:" + self.identity)
            if self.snapshot.active_sketch:
                result.data.update(active_sketch_geometry=[], active_sketch_geometry_complete=True)
        return result

    def candidate_request(self, operation, **payload):
        self.rpc.append(operation)
        if self.on_rpc:
            self.on_rpc(operation)
        if operation == "begin":
            self.source = (copy.deepcopy(self.snapshot), self.identity)
            self.number += 1
            self.identity = f"trial-{self.number}"
            self.snapshot.document_title = f"Candidate{self.number}"
            self.snapshot.document_path = f"C:/synthetic/Candidate{self.number}.sldprt"
            return {"ok": True, "token": f"token-{self.number}", "candidate": self.observe()}
        if not self.restore_ok:
            return {"ok": False, "status": "unavailable", "observation": self.observe()}
        if not payload["accept"]:
            self.snapshot, self.identity = copy.deepcopy(self.source)
        return {"ok": True, "status": "committed" if payload["accept"] else "restored",
                "observation": self.observe()}


def trial(radius=5, expected=1000, **updates):
    decision = act([call("sketch.begin", plane="front"), call("sketch.circle", radius_mm=radius),
                    call("feature.extrude", depth_mm=10)], goals=[])
    decision.trial = TrialSpec(hypothesis="改变轮廓后重新测量", checks=[goal(id="local", expected=expected)])
    return decision.model_copy(update=updates)


def setup_task(tmp_path, decisions):
    worker = CandidateWorker()
    backend = Backend([act([call("session.ensure"), call("document.new_part")]), *decisions])
    task = ContinuousTaskRunner(build_tool_registry(), worker, backend, TaskStore(tmp_path / "tasks"))
    assert task.prepare("建立可验证几何")["status"] == "ready"
    return task, worker, backend


def test_changed_failed_candidate_restored_and_revised_program_completes(tmp_path):
    task, worker, backend = setup_task(tmp_path, [trial(5), trial(4), complete()])
    def fail_first(command, before):
        if worker.number == 1:
            worker.snapshot.feature_count += 1
            worker.snapshot.volume_m3 = 5e-7
            return StepResult(False, command, "synthetic invalid geometry", error_code="GeometryError",
                              before=before, after=copy.deepcopy(worker.snapshot))
        del worker.fail["feature.extrude"]
        return worker.call(command)
    worker.fail["feature.extrude"] = fail_first
    result = task.run()
    assert result["status"] == "completed", result["message"]
    assert [item["status"] for item in result["trial_history"]] == ["restored", "committed"]
    assert worker.rpc == ["begin", "finish", "begin", "finish"]
    assert len(result["completed_steps"]) == 5
    assert [item["arguments"]["radius_mm"] for item in result["completed_steps"]
            if item["tool"] == "sketch.circle"] == [4]
    retry_context = backend.contexts[2]
    assert len(retry_context["completed_steps"]) == 2
    assert retry_context["trial_history"][0]["failure"]["message"] == "synthetic invalid geometry"
    assert retry_context["latest_observation"]["snapshot"]["volume_m3"] == 0


def test_failed_local_geometry_check_restores_without_weakening_final_goals(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(5, expected=9000), trial(4), complete()])
    result = task.run()
    assert result["status"] == "completed", result["message"]
    assert result["trial_history"][0]["checks"][0]["passed"] is False
    assert result["goals"][0]["expected"] == 1000
    assert len(result["evidence"]) == 5
    assert worker.rpc.count("finish") == 2


def test_relabel_or_weaken_check_cannot_replay_same_failed_program(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(5, expected=9000), trial(5), trial(4), complete()])
    result = task.run()
    assert result["status"] == "completed", result["message"]
    assert worker.rpc.count("begin") == 2
    assert any(item["kind"] == "repeat_blocked" for item in result["feedback"])


def test_stop_during_begin_never_runs_candidate_or_restoration(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(), complete()])
    worker.on_rpc = lambda op: task.stop() if op == "begin" else None
    result = task.run()
    assert result["status"] == "stopped"
    assert worker.rpc == ["begin"]
    assert "sketch.begin" not in worker.calls
    assert result["active_trial"]


def test_stop_during_step_never_triggers_hidden_restore(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(), complete()])
    worker.callback = lambda command: task.stop() if command.tool == "sketch.circle" else None
    result = task.run()
    assert result["status"] == "stopped"
    assert worker.rpc == ["begin"]
    assert "feature.extrude" not in worker.calls


def test_unverified_restoration_stops_before_next_program(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(expected=9000), trial(4), complete()])
    worker.restore_ok = False
    result = task.run()
    assert result["status"] == "review"
    assert worker.rpc == ["begin", "finish"]
    assert result["active_trial"]


def test_user_document_switch_is_not_automatically_reversed(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(), trial(4), complete()])
    def switch(command):
        if command.tool == "sketch.circle":
            worker.identity = "user-document"
            worker.snapshot.document_title = "UserExistingPart"
    worker.callback = switch
    result = task.run()
    assert result["status"] == "review"
    assert worker.rpc == ["begin"]
    assert worker.snapshot.document_title == "UserExistingPart"


def test_trial_does_not_bypass_required_final_geometry(tmp_path):
    task, _, _ = setup_task(tmp_path, [trial(expected=1000), complete()])
    task._task["goals"][0]["expected"] = 9000
    result = task.run()
    assert result["status"] != "completed"
    assert result["trial_history"][0]["status"] == "committed"
    assert result["verification"]["passed"] is False


def test_enabled_runtime_rejects_direct_write_without_trial(tmp_path):
    task, worker, _ = setup_task(tmp_path, [act([call("sketch.begin", plane="front")], goals=[]), trial(), complete()])
    result = task.run()
    assert result["status"] == "completed"
    assert worker.calls.count("sketch.begin") == 1
    assert any(item["kind"] == "policy_rejected" for item in result["feedback"])


@pytest.mark.parametrize("tool,args", [("document.new_part", {}), ("session.ensure", {}),
                                       ("sketch.begin_face", {})])
def test_trial_cannot_initialize_or_inherit_foreign_references(tmp_path, tool, args):
    invalid = trial()
    invalid.steps.insert(0, call(tool, **args))
    task, worker, _ = setup_task(tmp_path, [invalid, trial(), complete()])
    result = task.run()
    assert result["status"] == "completed"
    assert worker.rpc.count("begin") == 1


def test_trial_schema_requires_fixed_geometric_checks():
    with pytest.raises(ValidationError):
        TrialSpec(hypothesis="test", checks=[goal(metric="body_count", expected=1)])
    with pytest.raises(ValidationError):
        TrialSpec(hypothesis="test", checks=[goal(metric="dimension_mm", target="", binding="pending")])
    with pytest.raises(ValidationError):
        TaskDecision(action="inspect", reason="test", trial=trial().trial)


def test_six_different_failed_trials_do_not_reset_no_progress_budget(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(radius, expected=9000) for radius in range(1, 9)])
    result = task.run()
    assert result["status"] == "review"
    assert result["no_progress"] == 6
    assert worker.rpc.count("finish") == 6
    assert len(result["completed_steps"]) == 2
    recovery = [item for item in result["events"] if item["name"] == "trial_restored"]
    assert recovery[-1]["automatic_continuation"] is False
    assert "自动推进已暂停" in recovery[-1]["message"]
    assert "Agent 将" not in recovery[-1]["message"]
    assert all(item["automatic_continuation"] is True for item in recovery[:-1])


def test_stop_during_recovery_feedback_emits_no_late_continuation_promise(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(expected=9000), trial(4), complete()])

    def on_event(name, payload):
        if name == "task_feedback" and payload.get("kind") == "trial_restored":
            task.stop()

    task.on_event = on_event
    result = task.run()
    assert result["status"] == "stopped"
    assert worker.rpc == ["begin", "finish"]
    assert not any(item["name"] == "trial_restored" for item in result["events"])


def test_partial_preflight_rejection_cannot_commit_existing_geometry(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(expected=0), trial(4), complete()])
    def preflight(command):
        if worker.number == 1 and command.tool == "sketch.circle":
            return False, "暂时忙碌"
        return True, ""
    task.preflight = preflight
    result = task.run()
    assert result["status"] == "review", result["message"]
    assert result["last_failure"]["failure_category"] == "safety"
    assert worker.rpc == ["begin"]
    assert "sketch.circle" not in worker.calls
    assert "feature.extrude" not in worker.calls


def test_unfinished_trial_cannot_resume_after_restart(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(), complete()])
    worker.on_rpc = lambda op: task.stop() if op == "begin" else None
    result = task.run()
    restored = ContinuousTaskRunner(build_tool_registry(), CandidateWorker(), Backend([trial()]), task.store)
    restored.restore(result["id"])
    restored.resume("继续")
    assert restored.run()["status"] == "review"
    assert restored.worker.rpc == []


def test_stop_interrupts_cad_before_waiting_for_agent_process(tmp_path):
    task, worker, backend = setup_task(tmp_path, [trial()])
    def close():
        assert worker.interrupted is True
    backend.close = close
    task.stop()
    assert task.snapshot()["status"] == "stopped"
