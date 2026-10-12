from __future__ import annotations

import json
import copy

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

from sw_assistant.agent.pi_client import PiDecisionLoopExceeded
from sw_assistant.agent.task_backend import INITIALIZATION_EXAMPLE, TaskDecisionInvalid
from sw_assistant.agent.task_models import TaskDecision
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.domain import ModelSnapshot

from test_continuous_task_runner import act, call, goal, runner
from test_task_backend import FakePi, backend


def test_initialization_example_is_valid_without_observed_feature_names(tmp_path):
    decision = TaskDecision.model_validate(INITIALIZATION_EXAMPLE["decision"])
    task, worker, model = runner(tmp_path, [decision])
    value = task.prepare("为新零件建立一个主体拉伸特征", {"available": False})
    assert value["status"] == "ready"
    assert worker.calls == []
    assert value["goals"][0]["binding"] == "pending"
    assert value["requirements"][0]["goal_ids"] == ["main_feature"]
    context = model.contexts[0]
    assert context["startup"]["suggested_initial_steps"] == ["session.ensure", "document.new_part"]
    assert context["startup"]["event_bridge_unavailable_is_not_cad_failure"] is True
    assert context["remaining_budget"]["round_investigations"] == 8
    task.run(single_stage=True)
    assert task._context()["startup"]["phase"] == "modeling"
    assert task._context()["startup"]["suggested_initial_steps"] == []
    assert worker.calls.count("document.new_part") == 1


def test_native_empty_observation_updates_diagnostic_context_but_never_checkpoint(tmp_path):
    task, worker, model = runner(tmp_path, [act([call("session.ensure"), call("document.new_part")])])
    outputs = []

    def query():
        outputs.append(task.investigate("model.observe", {}))
        outputs.append(task.investigate("diagnostics.recent", {}))

    model.callback = query
    value = task.prepare("创建一个零件")
    assert value["status"] == "ready"
    assert value["latest_observation"]["snapshot"]["connected"] is False
    assert value["expected_observation"] == {}
    assert outputs[1]["data"]["state"]["connected"] is False
    assert outputs[1]["data"]["state"]["document_type"] == "none"
    assert worker.calls == ["model.observe"]


def test_existing_document_names_cannot_bind_before_task_document_creation(tmp_path):
    old_name = goal(id="depth", metric="dimension_mm", target="D1@ExistingBoss", expected=10)
    actual_name = old_name.model_copy(update={"target": "D1@NewTaskBoss"})
    finished = TaskDecision(action="complete", reason="读取到任务文档实际尺寸", goals=[goal(), actual_name])
    task, worker, backend = runner(tmp_path, [act(goals=[goal(), old_name]), finished])
    worker.snapshot = ModelSnapshot(connected=True, document_title="ExistingUserPart", document_type="part",
                                    feature_count=4, body_count=1, volume_m3=0.000042)
    worker.dimensions = [{"name": old_name.target, "value_mm": 10}]
    observed = []

    def first_read():
        if not observed:
            observed.append(task.investigate("model.observe", {}))

    def changing_documents(c):
        if c.tool == "document.new_part":
            worker.dimensions = []
        elif c.tool == "feature.extrude":
            worker.dimensions = [{"name": actual_name.target, "value_mm": 10}]

    backend.callback = first_read
    worker.callback = changing_documents
    prepared = task.prepare("创建一个深十毫米的全新零件")
    assert observed[0]["data"]["snapshot"]["document_title"] == "ExistingUserPart"
    assert prepared["goals"][1]["binding"] == "pending"
    assert "depth" not in prepared["goal_bindings"]
    original_numeric_contract = {key: prepared["goals"][1][key] for key in ("metric", "operator", "expected", "tolerance")}
    completed = task.run()
    assert completed["status"] == "completed", completed["message"]
    assert completed["goal_bindings"]["depth"]["target"] == actual_name.target
    assert {key: completed["goals"][1][key] for key in original_numeric_contract} == original_numeric_contract
    assert not any(item.get("target") == old_name.target for item in completed["contract_audit"])


def test_foreign_observation_cannot_rebind_names_before_identity_reconciliation(tmp_path):
    task, worker, _ = runner(tmp_path, [act()])
    task.prepare("创建零件")
    task.run(single_stage=True)
    expected = task.snapshot()["expected_observation"]
    foreign = copy.deepcopy(expected)
    foreign["snapshot"]["document_title"] = "AnotherDocument"
    assert task._goal_binding_observation(foreign) == {}
    assert task._goal_binding_observation(expected) == expected


def test_backend_explains_native_investigation_and_host_proposals_separately(tmp_path):
    service = backend(tmp_path)
    service.set_investigator(lambda _tool, _args: {"ok": True, "data": {"connected": False, "complete": True}})
    disallowed = service._investigate("session.ensure", {})
    assert disallowed["error_code"] == "ProposalRequired"
    assert "action=act" in disallowed["next_action"]
    first = service._investigate("session.capabilities", {})
    assert first["decision_protocol"]["round_investigations_remaining"] == 7
    assert first["decision_protocol"]["initialization_required"] is True
    assert first["decision_protocol"]["avoid_repeat_empty_inspections"] is True
    client = FakePi([json.dumps(INITIALIZATION_EXAMPLE["decision"])])
    service._pi = client
    assert service.decide({}).action == "act"
    payload = json.loads(client.prompts[0])
    assert "session.ensure" not in payload["investigation_tools"]
    assert any(item["name"] == "session.ensure" for item in payload["available_tools"])
    assert payload["initialization_example"]["decision"]["action"] == "act"


def test_truncated_success_prefix_does_not_suggest_creating_another_document(tmp_path):
    service = backend(tmp_path)
    service.set_investigator(lambda _tool, _args: {"ok": True, "data": {"connected": False}})
    service._pi = FakePi([json.dumps({"action": "inspect", "reason": "先重新核对连接"})])
    service.decide({"startup": {"phase": "modeling"},
                    "completed_summary": {"by_tool": {"document.new_part": 1}},
                    "completed_steps": [{"tool": "feature.extrude"}]})
    result = service._investigate("session.capabilities", {})
    assert "initialization_required" not in result["decision_protocol"]


def loop_error(message):
    return PiDecisionLoopExceeded(message, attempt_count=10, investigation_count=8)


def _with_real_backend(tmp_path, responses):
    source, worker, _ = runner(tmp_path, [])
    service = backend(tmp_path / "agent")
    task = ContinuousTaskRunner(source.registry, worker, service, source.store)
    service.set_investigator(task.investigate)
    client = FakePi(responses)
    service._pi = client
    return task, worker, service, client


def test_pi_investigation_loop_returns_to_outer_correction_and_initializes(tmp_path):
    task, worker, service, client = _with_real_backend(tmp_path, [
        loop_error("调查用完后仍未返回 JSON"), json.dumps(INITIALIZATION_EXAMPLE["decision"]),
    ])
    value = task.prepare("为新零件创建主体拉伸")
    assert value["status"] == "ready"
    assert value["rounds"] == 2
    assert service.backend_source == "pi" and not client.closed
    assert worker.calls == []
    assert any(item["kind"] == "decision_rejected" for item in value["feedback"])
    task.run(single_stage=True)
    assert worker.calls.count("session.ensure") == 1
    assert worker.calls.count("document.new_part") == 1


def test_persistent_pi_nondecisions_are_bounded_before_all_48_rounds(tmp_path):
    task, worker, service, client = _with_real_backend(tmp_path, [loop_error("仍然重复空调查")] * 20)
    result = task.prepare("为新零件创建主体拉伸")
    assert result["status"] == "review"
    assert result["rounds"] == result["failure_budget"]["counts"]["protocol"] == 6
    assert result["no_progress"] == 0
    assert "最近阻碍" in result["message"] and "阶段决策" in result["message"]
    assert len(client.prompts) == 6
    assert service.backend_source == "pi"
    assert worker.calls == []


def test_builtin_request_budget_is_candidate_correction_not_configuration_failure(tmp_path):
    service = backend(tmp_path, selected="pydantic")

    async def exhausted(_prompt):
        raise UsageLimitExceeded("requests limit exceeded")

    service._run_pydantic = exhausted
    with pytest.raises(TaskDecisionInvalid, match="本轮模型调用预算"):
        service.decide({})


def test_public_response_progress_contains_counts_not_model_reasoning(tmp_path):
    service = backend(tmp_path)
    events = []
    service.on_event = lambda stage, message, data: events.append((stage, message, data))
    service._on_pi_event("message_end", {"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private reasoning"}]}})
    assert service.progress["observed_model_responses"] == 1
    assert events[0][0] == "agent_response_progress"
    assert "private reasoning" not in json.dumps(events)
