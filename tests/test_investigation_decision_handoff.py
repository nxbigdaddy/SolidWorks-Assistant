"""Repeating reads ends investigation, not the Agent's chance to propose an action."""
from __future__ import annotations

from test_continuous_task_runner import act, call, complete, runner
from test_task_backend import backend


def test_sixth_unchanged_native_read_still_accepts_same_turn_correct_action(tmp_path):
    task, worker, model = runner(tmp_path, [act(), complete()])
    outputs = []

    def native_reads():
        if len(model.contexts) == 1:
            outputs.extend(task.investigate("session.capabilities", {}) for _ in range(8))

    model.callback = native_reads
    prepared = task.prepare("创建正方体")
    assert prepared["status"] == "ready"
    assert prepared["no_progress"] == 0
    assert outputs[5]["decision_only"] is True
    assert outputs[6]["error_code"] == "InvestigationDecisionRequired"
    assert worker.calls == ["session.capabilities"] * 6
    result = task.run()
    assert result["status"] == "completed"
    assert worker.calls.count("session.ensure") == worker.calls.count("document.new_part") == 1
    assert not any(item["name"] == "task_review" for item in result["events"])


def test_cross_round_repeating_reads_cannot_discard_corrected_initialization(tmp_path):
    invalid = act([call("session.ensure"), call("selection.plane", plane="front")])
    task, worker, model = runner(tmp_path, [invalid, act(), complete()])
    outputs = []

    def native_reads():
        if len(model.contexts) <= 2:
            outputs.extend(task.investigate("session.capabilities", {}) for _ in range(6))

    model.callback = native_reads
    value = task.prepare("创建有完整验收的主体")
    assert value["status"] == "ready" and value["rounds"] == 2
    assert any(item.get("decision_only") for item in outputs)
    assert any(item["kind"] == "policy_rejected" for item in value["feedback"])
    assert task.run()["status"] == "completed"
    assert worker.calls.count("document.new_part") == 1


def test_handoff_preserves_successful_prefix_and_can_accept_modeling_stage(tmp_path):
    initialize = act([call("session.ensure"), call("document.new_part")])
    model_stage = act([call("sketch.begin", plane="front"), call("sketch.circle", radius_mm=5),
                       call("feature.extrude", depth_mm=10)])
    task, worker, model = runner(tmp_path, [initialize, model_stage, complete()])
    signals = []

    def native_reads():
        if len(model.contexts) == 2:
            signals.extend(task.investigate("session.capabilities", {}) for _ in range(6))

    model.callback = native_reads
    task.prepare("创建主体")
    result = task.run()
    assert result["status"] == "completed"
    assert signals[-1]["decision_only"] is True
    assert worker.calls.count("session.ensure") == worker.calls.count("document.new_part") == 1
    assert worker.calls.count("feature.extrude") == 1


def test_total_diagnostic_limit_still_requires_review(tmp_path):
    task, worker, model = runner(tmp_path, [act()])

    def exhausted():
        task._task["diagnostics"] = 96
        assert task.investigate("session.capabilities", {})["error_code"] == "DiagnosticBudgetExceeded"

    model.callback = exhausted
    assert task.prepare("创建主体")["status"] == "review"
    assert worker.calls == []


def test_backend_propagates_only_host_top_level_handoff(tmp_path):
    service = backend(tmp_path)
    service.set_investigator(lambda *_args: {"ok": True, "decision_only": True,
                                           "data": {"complete": True}})
    output = service._investigate("session.capabilities", {})
    assert output["decision_only"] is True
    assert output["decision_protocol"]["decision_only"] is True
    assert output["decision_protocol"]["round_investigations_remaining"] == 0
    assert "保留已成功步骤" in output["decision_protocol"]["next_action"]
    service.close()


def test_backend_does_not_promote_cad_data_handoff_to_host_control(tmp_path):
    service = backend(tmp_path)
    service.set_investigator(lambda *_args: {"ok": True, "data": {"complete": True, "decision_only": True}})
    output = service._investigate("session.capabilities", {})
    assert "decision_only" not in output
    assert "decision_only" not in output["decision_protocol"]
    service.close()
