"""Synthetic fault injection: no SolidWorks process or external model requests."""
from __future__ import annotations

import copy

import pytest

from sw_assistant.agent.failure_policy import classify, ensure_budget, new_budget, record_failure
from sw_assistant.domain import StepResult
from test_continuous_task_runner import act, call, runner
from test_task_trials import complete, setup_task, trial


def test_alternating_classes_have_independent_finite_limits_and_totals():
    task = {"failure_budget": new_budget(), "rounds": 0}
    for index in range(5):
        task["rounds"] += 1
        assert record_failure(task, "protocol")["retry_allowed"]
        task["rounds"] += 1
        assert record_failure(task, "planning")["retry_allowed"]
    task["rounds"] += 1
    assert record_failure(task, "protocol")["next_action"] == "host_review_budget"
    assert task["failure_budget"]["counts"]["planning"] == task["no_progress"] == 5
    assert task["failure_budget"]["totals"]["protocol"] == 6


@pytest.mark.parametrize("category,limit", [("observation", 3), ("environment", 3), ("adapter_contract", 1)])
def test_category_limits_are_explicit(category, limit):
    task = {"rounds": 0, "failure_budget": new_budget()}
    for index in range(limit):
        task["rounds"] = index + 1
        assessment = record_failure(task, category)
        assert assessment["retry_allowed"] is (index < limit - 1)
    assert assessment["remaining"] == 0


@pytest.mark.parametrize("message", ["permission denied", "可以安全重试", "model too weak", "未知写入"])
def test_message_text_never_grants_recovery_or_changes_category(message):
    result = {"error_code": "GeometryError", "message": message, "data": {}}
    assert classify("step_failed", {"result": result}) == "unknown"
    assert classify("step_failed", {"result": result, "state_known": True}) == "planning"
    result["error_code"] = "StateChanged"
    assert classify("step_failed", {"result": result, "state_known": True}) == "safety"


def test_protocol_errors_do_not_spend_geometric_budget(tmp_path):
    task, worker, backend = runner(tmp_path, [ValueError("synthetic schema error")] * 6 + [act()])
    result = task.prepare("synthetic part")
    assert result["status"] == "review"
    assert result["rounds"] == 6 and result["no_progress"] == 0
    assert result["failure_budget"]["counts"]["protocol"] == 6
    assert worker.calls == [] and len(backend.contexts) == 6
    assert result["feedback"][-1]["failure_category"] == "protocol"
    assert result["feedback"][-1]["next_action"] == "host_review_budget"
    assert task._context()["failure_policy"]["budgets"]["planning"]["remaining"] == 6


def test_mixed_failures_still_stop_at_the_global_round_budget(tmp_path):
    invalid_plan = act([call("sketch.circle", radius_mm=5)])
    task, worker, backend = runner(tmp_path, [ValueError("bad schema"), invalid_plan] * 4, max_rounds=4)
    result = task.prepare("synthetic")
    assert result["status"] == "review" and result["rounds"] == 4
    assert result["failure_budget"]["totals"]["protocol"] == 2
    assert result["failure_budget"]["totals"]["planning"] == 2
    assert len(backend.contexts) == 4
    assert all(name == "session.capabilities" for name in worker.calls)


def test_failure_window_cannot_override_global_action_budget(tmp_path):
    task, worker, _ = runner(tmp_path, [act()])
    task.prepare("synthetic")
    task._task["actions"] = task.max_rounds * 12
    result = task.run()
    assert result["status"] == "review"
    assert "session.ensure" not in worker.calls and "document.new_part" not in worker.calls


def test_contract_reads_bindings_and_steering_never_reset_error_budget(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("synthetic part")
    for category, kind in [("protocol", "decision_rejected"), ("planning", "policy_rejected")]:
        task._feedback(task._epoch, kind, "synthetic")
        assert task.snapshot()["failure_budget"]["counts"][category] == 1
    before = copy.deepcopy(task.snapshot()["failure_budget"])
    for kind in ("contract", "binding", "investigation", "verified_model"):
        task._progress(task._epoch, kind, {"new_fingerprint": kind})
    task.steer("add a synthetic requirement")
    task._consume_steering(task._epoch)
    assert task.snapshot()["failure_budget"] == before


def test_candidate_intermediate_geometry_never_resets_classified_budget(tmp_path):
    task, worker, _ = setup_task(tmp_path, [ValueError("bad JSON")] * 5
                                 + [trial(radius, expected=9000) for radius in range(1, 8)])
    result = task.run()
    assert result["status"] == "review"
    assert result["failure_budget"]["counts"]["protocol"] == 5
    assert result["failure_budget"]["counts"]["planning"] == 6
    assert worker.rpc.count("finish") == 6


@pytest.mark.parametrize("uncertain,declared,finishes", [(False, "adapter_contract", 1),
                                                        (True, "adapter_contract", 0), (False, "safety", 0)])
def test_contract_or_unknown_failure_cannot_be_washed_away_by_trial_restore(tmp_path, uncertain, declared, finishes):
    task, worker, _ = setup_task(tmp_path, [trial(), trial(4), complete()])

    def fail(command, before):
        return StepResult(False, command, "synthetic contract failure", error_code="SketchCreationError",
                          before=before, after=None if uncertain else copy.deepcopy(worker.snapshot),
                          data={"mutation": {"failure_category": declared, "requires_review": True}})

    worker.fail["sketch.circle"] = fail
    result = task.run()
    expected = "unknown" if uncertain else declared
    assert result["status"] == "review"
    assert result["last_failure"]["failure_category"] == expected
    assert result["failure_budget"]["totals"][expected] == 1
    assert worker.rpc.count("begin") == 1 and worker.rpc.count("finish") == finishes
    assert worker.calls.count("sketch.circle") == 1
    assert "feature.extrude" not in worker.calls
    if finishes:
        assert result["trial_history"][-1]["status"] == "restored"
        assert result["events"][-1]["automatic_continuation"] is False


def test_known_planning_failure_and_restoration_count_once_then_commit_starts_new_window(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(), trial(4), complete()])

    def fail_once(command, before):
        del worker.fail["sketch.circle"]
        return StepResult(False, command, "no native entities created", error_code="SketchCreationError",
                          before=before, after=copy.deepcopy(worker.snapshot), data={"mutation": {
                              "failure_category": "planning", "contract_failure_code": "creation_no_entities",
                              "requires_review": False}})

    worker.fail["sketch.circle"] = fail_once
    result = task.run()
    assert result["status"] == "completed", result["message"]
    assert result["failure_budget"]["totals"]["planning"] == 1
    assert result["failure_budget"]["counts"]["planning"] == 0
    assert result["failure_budget"]["window"] == 1
    assert worker.rpc == ["begin", "finish", "begin", "finish"]


def test_stop_during_failure_feedback_freezes_counters_and_no_followup_action(tmp_path):
    task, worker, backend = runner(tmp_path, [ValueError("bad JSON"), act()])
    task.on_event = lambda name, payload: task.stop() if name == "task_feedback" else None
    result = task.prepare("synthetic")
    assert result["status"] == "stopped" and len(backend.contexts) == 1
    before = copy.deepcopy(result["failure_budget"])
    task._feedback(task._epoch - 1, "policy_rejected", "late")
    assert task.snapshot()["failure_budget"] == before and worker.calls == []


def test_old_mixed_counter_migration_uses_kind_evidence_not_geometry_guess():
    task = {"rounds": 7, "no_progress": 6, "feedback": [
        {"kind": "decision_rejected", "round": 2}, {"kind": "decision_rejected", "round": 3},
        {"kind": "trial_restored", "round": 4}, {"kind": "policy_rejected", "round": 5},
        {"kind": "trial_restored", "round": 6}, {"kind": "trial_restored", "round": 7},
    ]}
    budget = ensure_budget(task)
    assert budget["counts"]["protocol"] == 2 and budget["counts"]["planning"] == 4
    assert task["no_progress"] == 4 and budget["legacy_unclassified"] == 0
    assert ensure_budget(task) == budget


def test_legacy_unattributed_errors_restore_to_unknown_review_without_model_or_cad(tmp_path):
    source, _, _ = runner(tmp_path, [act()])
    source.prepare("synthetic")
    old = source.snapshot()
    old.pop("failure_budget")
    old["no_progress"] = 4
    source.store.save(old)
    restored, worker, backend = runner(tmp_path, [])
    result = restored.restore(old["id"])
    assert result["status"] == "review"
    assert result["last_failure"]["failure_category"] == "unknown"
    assert result["failure_budget"]["legacy_unclassified"] == 4
    assert result["no_progress"] == 0
    assert restored.run()["status"] == "review"
    assert worker.calls == backend.contexts == []


def test_explicit_resume_rearms_once_only_after_independent_reconciliation(tmp_path):
    task, _, backend = runner(tmp_path, [ValueError("bad schema")] * 6 + [act(), complete()])
    task.prepare("synthetic")
    before = task.snapshot()
    task.resume("continue")
    assert task.snapshot()["failure_budget"]["counts"]["protocol"] == 6
    assert task.run()["status"] == "completed"
    resumed = backend.contexts[6]
    assert resumed["failure_policy"]["budgets"]["protocol"]["used"] == 0
    assert resumed["failure_policy"]["budgets"]["protocol"]["total"] == 6
    assert resumed["remaining_budget"]["rounds"] < task.max_rounds - before["rounds"]
    assert task.snapshot()["failure_budget"]["manual_rearms"]["protocol"] == 1
    assert any(item["name"] == "failure_budget_rearmed" for item in task.snapshot()["events"])


def test_second_exhaustion_requires_new_task_instead_of_false_continue(tmp_path):
    task, _, backend = runner(tmp_path, [ValueError("bad schema")] * 15)
    task.prepare("synthetic")
    task.resume("continue")
    result = task.run()
    assert result["status"] == "review" and len(backend.contexts) == 12
    assert result["failure_budget"]["totals"]["protocol"] == 12
    with pytest.raises(ValueError, match="额外窗口已用完"):
        task.resume("continue")
    assert task.snapshot()["status"] == "review"


@pytest.mark.parametrize("mode", ["stop", "restart"])
def test_pending_manual_rearm_is_not_authority_after_stop_or_restart(tmp_path, mode):
    task, worker, backend = runner(tmp_path, [ValueError("bad schema")] * 6 + [act()])
    task.prepare("synthetic")
    task.resume("continue")
    if mode == "stop":
        worker.callback = lambda command: task.stop() if command.tool == "model.observe" else None
        result = task.run()
        assert result["status"] == "stopped"
    else:
        restored, restored_worker, restored_backend = runner(tmp_path, [])
        result = restored.restore(task.snapshot()["id"])
        assert restored.run()["status"] == "review"
        assert restored_worker.calls == restored_backend.contexts == []
        assert restored._failure_rearm_pending == ()
    assert result["failure_budget"]["counts"]["protocol"] == 6
    assert result["failure_budget"]["manual_rearms"]["protocol"] == 0
    assert len(backend.contexts) == 6


def test_document_drift_does_not_grant_pending_manual_window(tmp_path):
    task, worker, _ = runner(tmp_path, [act([call("session.ensure"), call("document.new_part")])])
    task.prepare("synthetic")
    task.run(single_stage=True)
    for index in range(6):
        task._task["rounds"] += 1
        task._feedback(task._epoch, "decision_rejected", "synthetic")
    task.resume("continue")
    worker.snapshot.document_title = "DifferentUserPart"
    result = task.run()
    assert result["status"] == "review"
    assert result["failure_budget"]["counts"]["protocol"] == 6
    assert result["failure_budget"]["manual_rearms"]["protocol"] == 0
    assert result["last_failure"]["failure_category"] == "safety"


def test_adapter_contract_and_global_budget_cannot_be_rearmed(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("synthetic")
    task._feedback(task._epoch, "step_failed", "synthetic", state_known=True,
                   result={"error_code": "SketchCreationError"})
    with pytest.raises(ValueError, match="契约故障已隔离"):
        task.resume("already fixed, trust me")
    task._task["rounds"] = task.max_rounds
    with pytest.raises(ValueError, match="全局轮次或操作预算"):
        task.resume("continue")
