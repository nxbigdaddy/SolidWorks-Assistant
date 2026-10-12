"""Native modeling stages cannot bypass runner ownership, trials or final goals."""
from __future__ import annotations

import copy
import json
import threading

import pytest

from sw_assistant.agent.task_models import TaskDecision
from sw_assistant.domain import StepResult
from test_continuous_task_runner import act, call, complete, runner
from test_task_trials import setup_task, trial


def envelope(decision, request_id="one"):
    return {"decision": decision.model_dump(mode="json"), "request_id": request_id}


def test_native_failed_trial_restores_then_revised_program_commits_in_same_decide(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []

    def fail_first(command, before):
        if worker.number == 1:
            worker.snapshot.feature_count += 1
            worker.snapshot.volume_m3 = 5e-7
            return StepResult(False, command, "synthetic invalid geometry", error_code="GeometryError",
                              before=before, after=copy.deepcopy(worker.snapshot))
        del worker.fail["feature.extrude"]
        return worker.call(command)

    worker.fail["feature.extrude"] = fail_first

    def native_turn():
        assert task._context()["agent_stage_execution"]["allowed"] is True
        outputs.append(task.execute_agent_stage(envelope(trial(5))))
        outputs.append(task.execute_agent_stage(envelope(trial(4), "two")))

    model.callback = native_turn
    result = task.run()
    assert result["status"] == "completed", result["message"]
    assert [item["status"] for item in outputs] == ["trial_restored", "trial_committed"]
    assert [item["status"] for item in result["trial_history"]] == ["restored", "committed"]
    assert result["actions"] == 8 and len(result["completed_steps"]) == 5
    assert worker.rpc == ["begin", "finish", "begin", "finish"]
    assert not task._native_run_scope and not task._native_decide_active


def test_native_stage_prepare_and_idle_and_other_thread_are_cad_free(tmp_path):
    initial = act([call("session.ensure"), call("document.new_part")])
    task, worker, model = runner(tmp_path, [initial, complete()])
    outputs = []
    model.callback = lambda: outputs.append(task.execute_agent_stage(envelope(initial)))
    task.prepare("create synthetic part")
    assert worker.calls == [] and outputs[0]["error_code"] == "StageScopeDenied"
    assert task._context()["agent_stage_execution"]["allowed"] is False
    assert task.execute_agent_stage(envelope(initial))["error_code"] == "StageScopeDenied"

    def callback():
        thread = threading.Thread(target=lambda: outputs.append(task.execute_agent_stage(envelope(initial))))
        thread.start()
        thread.join(3)

    model.callback = callback
    task.run(single_stage=True)
    task.run()
    assert outputs[-1]["error_code"] == "StageScopeDenied"
    assert worker.calls.count("document.new_part") == 1


def test_duplicate_native_request_returns_cache_without_replaying_trial(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []
    request = envelope(trial())

    def callback():
        outputs.append(task.execute_agent_stage(request))
        outputs.append(task.execute_agent_stage(request))

    model.callback = callback
    result = task.run()
    assert result["status"] == "completed"
    assert outputs[0] == outputs[1]
    assert worker.rpc == ["begin", "finish"]
    stored = task.store.load(result["id"])
    assert stored["agent_stage_requests"]["one"]["status"] == "trial_committed"


def test_native_result_followed_by_same_final_act_is_not_replayed(tmp_path):
    candidate = trial()
    task, worker, model = setup_task(tmp_path, [candidate, complete()])

    def callback():
        model.callback = None
        assert task.execute_agent_stage(envelope(candidate))["status"] == "trial_committed"

    model.callback = callback
    result = task.run()
    assert result["status"] == "completed"
    assert worker.rpc == ["begin", "finish"]
    assert any(item["kind"] == "native_stage_handoff" for item in result["feedback"])
    assert len(result["completed_steps"]) == 5


def test_native_stage_cannot_weaken_locked_goals(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []

    def callback():
        candidate = trial().model_dump(mode="json")
        candidate["goals"] = [task_goal := copy.deepcopy(task._task["goals"][0])]
        task_goal["expected"] = 1
        outputs.append(task.execute_agent_stage({"decision": candidate, "request_id": "weak"}))

    model.callback = callback
    result = task.run()
    assert result["status"] != "completed"
    assert outputs[0]["status"] == "denied"
    assert result["goals"][0]["expected"] == 1000 and worker.rpc == []


def test_native_stage_rejects_direct_geometry_outside_isolated_trial(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []

    def callback():
        candidate = trial().model_copy(update={"trial": None})
        outputs.append(task.execute_agent_stage(envelope(candidate)))

    model.callback = callback
    result = task.run()
    assert result["status"] != "completed"
    assert outputs[0]["error_code"] == "InvalidStageDecision" and worker.rpc == []


def test_single_stage_mode_executes_only_one_native_stage(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    assert task.run(single_stage=True)["status"] == "ready"  # legacy initialization
    outputs = []

    def callback():
        assert task._context()["agent_stage_execution"]["max_stages"] == 1
        outputs.append(task.execute_agent_stage(envelope(trial())))
        outputs.append(task.execute_agent_stage(envelope(trial(4), "two")))

    model.callback = callback
    result = task.run(single_stage=True)
    assert result["status"] == "ready"
    assert [item["status"] for item in outputs] == ["trial_committed", "denied"]
    assert worker.rpc == ["begin", "finish"]


def test_native_two_attempt_budget_includes_invalid_decisions(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []

    def callback():
        for index, candidate in enumerate([{"action": "act"}, {"action": "nope"}, trial().model_dump(mode="json")]):
            outputs.append(task.execute_agent_stage({"request_id": str(index), "decision": candidate}))

    model.callback = callback
    task.run()
    assert outputs[2]["error_code"] == "StageBudgetExceeded" and worker.rpc == []


def test_native_stage_uses_global_action_budget_before_starting_trial(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []

    def callback():
        task._task["actions"] = task.max_rounds * 12 - 2
        outputs.append(task.execute_agent_stage(envelope(trial())))

    model.callback = callback
    task.run()
    assert outputs[0]["error_code"] == "ActionBudgetExceeded" and worker.rpc == []


def test_stop_during_native_trial_leaves_unknown_and_never_restores(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []
    worker.on_rpc = lambda operation: task.stop() if operation == "begin" else None
    model.callback = lambda: outputs.append(task.execute_agent_stage(envelope(trial())))
    result = task.run()
    assert result["status"] == "stopped"
    assert outputs[0]["status"] == "stopped" and outputs[0]["requires_review"]
    assert worker.rpc == ["begin"] and "sketch.begin" not in worker.calls
    assert result["agent_stage_requests"]["one"]["result"]["status"] == "stopped"


def test_native_stage_watchdog_is_bounded_and_stops_on_owning_epoch(tmp_path, monkeypatch):
    from sw_assistant.agent import task_operations

    task, worker, model = setup_task(tmp_path, [complete()])
    timers = []

    class Timer:
        def __init__(self, seconds, callback):
            self.seconds, self.callback, self.cancelled = seconds, callback, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(task_operations.threading, "Timer", Timer)
    worker.on_rpc = lambda operation: timers[0].callback() if operation == "begin" else None
    model.callback = lambda: task.execute_agent_stage(envelope(trial()))
    result = task.run()
    assert result["status"] == "stopped" and worker.interrupted
    assert timers[0].seconds == 600 and timers[0].daemon and timers[0].cancelled


def test_native_stage_denies_stale_user_steering_before_cad(tmp_path):
    task, worker, model = setup_task(tmp_path, [complete()])
    outputs = []

    def callback():
        model.callback = None
        task.steer("增加一处新孔")
        outputs.append(task.execute_agent_stage(envelope(trial())))

    model.callback = callback
    task.run()
    assert outputs[0]["error_code"] == "StageContextStale" and worker.rpc == []


def test_native_initialization_is_possible_after_prepare_inspection_proposal(tmp_path):
    first = TaskDecision(action="inspect", reason="defer initialization until execution")
    task, worker, model = runner(tmp_path, [first, complete()])
    task.prepare("create synthetic body")
    outputs = []
    model.callback = lambda: outputs.append(task.execute_agent_stage(envelope(act([call("session.ensure"), call("document.new_part")]))))
    task.run(single_stage=True)
    assert outputs[0]["status"] == "passed"
    assert worker.calls.count("document.new_part") == 1


def test_partial_initialization_failure_is_not_reported_as_passed(tmp_path):
    first = TaskDecision(action="inspect", reason="defer initialization")
    task, worker, model = runner(tmp_path, [first, complete()])
    task.prepare("create synthetic body")
    outputs = []
    worker.fail["document.new_part"] = lambda command, before: StepResult(
        False, command, "synthetic template unavailable", error_code="TemplateError",
        before=before, after=copy.deepcopy(before),
    )
    model.callback = lambda: outputs.append(task.execute_agent_stage(envelope(act([call("session.ensure"), call("document.new_part")]))))
    task.run(single_stage=True)
    assert outputs[0]["status"] == "not_committed" and outputs[0]["ok"] is False
    assert outputs[0]["completed_step_count"] == 1


@pytest.mark.parametrize("changed", [True, False])
def test_native_used_request_id_cannot_reenter_another_round(tmp_path, changed):
    task, worker, model = setup_task(tmp_path, [TaskDecision(action="inspect", reason="inspect after stage"), complete()])
    outputs = []
    first = envelope(trial())

    def callback():
        request = envelope(trial(4)) if outputs and changed else first
        outputs.append(task.execute_agent_stage(request))

    model.callback = callback
    result = task.run()
    assert result["status"] == "completed"
    assert outputs[1]["error_code"] == "StageReplayRejected"
    assert worker.rpc == ["begin", "finish"]


def test_local_pi_backend_and_runner_recover_and_complete_without_extra_prompt(tmp_path):
    from sw_assistant.agent.task_backend import TaskAgentBackend
    from sw_assistant.infrastructure import ModelSettings
    from test_native_model_stage import stage_delta
    from test_pi_decision_loop import native_client

    task, worker, _ = setup_task(tmp_path, [])

    def respond(request, index):
        if index == 1:
            return stage_delta(1, trial(5, expected=9000).model_dump(mode="json"))
        if index == 2:
            assert "trial_restored" in json.dumps(request["messages"])
            return stage_delta(2, trial(4).model_dump(mode="json"))
        assert index == 3
        assert "trial_committed" in json.dumps(request["messages"])
        return {"role": "assistant", "content": complete().model_dump_json()}, "stop"

    with native_client(tmp_path / "pi-runtime", respond, lambda *args: {}) as (client, requests):
        service = TaskAgentBackend(ModelSettings(client.api_base, "synthetic", "local-fixture"),
                                   task.registry, tmp_path / "backend", session_id="offline-native", backend="pi")
        service.set_investigator(task.investigate)
        service.set_operator(task.execute_agent_stage)
        client.operator = service._operate
        client.investigator = service._investigate
        service._pi = client
        task.backend = service
        try:
            result = task.run()
        finally:
            service.close()
        assert result["status"] == "completed", result["message"]
        assert len(requests) == 3 and service._round == 1
        assert [item["status"] for item in result["trial_history"]] == ["restored", "committed"]
        assert result["verification"]["passed"] and result["goals"][0]["expected"] == 1000
        assert worker.rpc == ["begin", "finish", "begin", "finish"]
