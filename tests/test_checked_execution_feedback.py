"""Typed pre-dispatch failure feedback; fake worker only, no CAD or network."""
from __future__ import annotations

import copy

import pytest

from sw_assistant.domain import StepResult
from test_continuous_task_runner import act, complete, runner


def failing_task(tmp_path, execution):
    task, worker, backend = runner(tmp_path, [act(), complete()])

    def failed(command, before):
        return StepResult(False, command, "synthetic checked execution rejected", error_code="StateChanged",
                          before=before, after=copy.deepcopy(worker.snapshot), data={"execution": execution})

    worker.fail["sketch.circle"] = failed
    assert task.prepare("synthetic checked command")["status"] == "ready"
    return task, worker, backend


@pytest.mark.parametrize("execution,not_started", [
    pytest.param({"started": False}, True, id="boolean-false"),
    pytest.param({"started": True}, False, id="boolean-true"),
    pytest.param({"started": None}, False, id="null"),
    pytest.param({}, False, id="missing"),
    pytest.param({"started": 0}, False, id="integer-zero"),
    pytest.param({"started": "false"}, False, id="string-false"),
])
def test_only_explicit_boolean_false_may_claim_command_was_not_executed(tmp_path, execution, not_started):
    task, worker, backend = failing_task(tmp_path, execution)
    result = task.run()
    assert result["status"] == "review"
    assert result["last_failure"]["failure_category"] == "safety"
    assert result["last_failure"]["next_action"] == "host_review_no_replay"
    assert result["failure_budget"]["totals"]["safety"] == 1
    assert worker.calls.count("sketch.circle") == 1
    assert "feature.extrude" not in worker.calls and len(backend.contexts) == 1
    assert result["failure_attempts"][-1]["result"]["data"]["execution"] == execution
    if not_started:
        assert "尚未执行" in result["message"]
        assert "不要为本次命令执行撤销" in result["message"]
    else:
        assert "尚未执行" not in result["message"]
        assert "不要为本次命令执行撤销" not in result["message"]


def test_stop_during_typed_failure_feedback_wins_over_late_not_executed_notice(tmp_path):
    task, worker, backend = failing_task(tmp_path, {"started": False})

    def stop_on_feedback(name, payload):
        if name == "task_feedback" and payload.get("kind") == "step_failed":
            task.stop()

    task.on_event = stop_on_feedback
    result = task.run()
    assert result["status"] == "stopped"
    assert "已停止所有后续动作" in result["message"]
    assert "尚未执行" not in result["message"]
    assert result["events"][-1]["name"] == "task_stopped"
    assert not any(event["name"] == "task_review" for event in result["events"])
    assert result["failure_budget"]["totals"]["safety"] == 1
    assert worker.calls.count("sketch.circle") == 1
    assert "feature.extrude" not in worker.calls and len(backend.contexts) == 1
