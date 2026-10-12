"""Finish diagnostics remain useful without weakening candidate verification."""
from __future__ import annotations

import copy
import json

import pytest

from sw_assistant.agent.task_trials import _finish_issues, _finish_receipt
from test_continuous_task_runner import complete
from test_task_trials import setup_task, trial


def test_finish_failure_reason_is_preserved_in_active_trial_and_review(tmp_path):
    task, worker, _ = setup_task(tmp_path, [trial(), complete()])
    original = worker.candidate_request

    def request(operation, **payload):
        if operation != "finish":
            return original(operation, **payload)
        worker.rpc.append("finish")
        return {"ok": False, "status": "unavailable", "reason": "checkpoint save verification failed: mass signature mismatch",
                "error_code": "CheckpointSaveFailed", "persisted_current_state": False,
                "observation": worker.observe()}

    worker.candidate_request = request
    result = task.run()
    assert result["status"] == "review" and worker.rpc == ["begin", "finish"]
    assert "mass signature mismatch" in result["message"]
    receipt = result["active_trial"]["finish_receipt"]
    assert receipt["error_code"] == "CheckpointSaveFailed" and receipt["persisted_current_state"] is False
    assert "observation" not in receipt
    assert task.store.load(result["id"])["active_trial"]["finish_receipt"] == receipt


@pytest.mark.parametrize("field", ["body_count", "document_identity", "configuration_identity", "model_update_stamp"])
def test_finish_comparison_failure_names_the_changed_field(tmp_path, field):
    task, worker, _ = setup_task(tmp_path, [trial(), complete()])
    original = worker.candidate_request
    old_call = worker.call

    def call(command, timeout_seconds=120):
        result = old_call(command, timeout_seconds)
        if command.tool == "model.observe":
            result.data["model_update_stamp"] = 100
        return result

    worker.call = call

    def request(operation, **payload):
        result = original(operation, **payload)
        if operation == "finish":
            observation = copy.deepcopy(result["observation"])
            if field == "body_count":
                observation["snapshot"][field] = 2
            else:
                observation[field] = "changed" if field != "model_update_stamp" else 101
            result["observation"] = observation
        return result

    worker.candidate_request = request
    result = task.run()
    assert result["status"] == "review" and worker.rpc == ["begin", "finish"]
    assert field in result["message"]
    assert any(field in issue for issue in result["active_trial"]["finish_receipt"]["validation_issues"])


def test_finish_receipt_is_scalar_bounded_and_does_not_copy_paths_or_observation():
    result = {"ok": False, "status": "unavailable", "reason": "failure " * 1000,
              "error_code": ["not", "scalar"], "checkpoint_id": "a" * 32,
              "checkpoint_parent_id": None, "persisted_current_state": False,
              "observation": {"large": "x" * 100_000}, "checkpoint_path": "C:/private/part.sldprt"}
    receipt = _finish_receipt(result)
    assert len(receipt["reason"]) == 2000
    assert "private" not in json.dumps(receipt) and "observation" not in receipt
    assert receipt["error_code"] == "<invalid scalar>"
    assert receipt["persisted_current_state"] is False


def test_missing_or_malformed_finish_observation_never_passes():
    for value in (None, [], "not an object", {}):
        assert _finish_issues({"ok": True, "status": "committed", "observation": value}, {}, True)
    assert _finish_issues(None, {}, True) == ["finish 返回值不是对象"]
