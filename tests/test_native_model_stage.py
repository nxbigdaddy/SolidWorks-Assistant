"""Native stage transport uses synthetic endpoints and fake host callbacks only."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from sw_assistant.agent import pi_client as pi_module
from sw_assistant.agent.pi_client import PiAgentError, PiDecisionLoopExceeded, PiResponseTimeout, PiRpcClient, PiRuntime
from sw_assistant.agent.task_backend import TaskBackendUnavailable, TaskDecisionInvalid
from test_agent_response_budget import end_events, simulated_client, start_events, stream_delta
from test_pi_decision_loop import decision_response, native_client, tool_response
from test_task_backend import DECISION, FakePi, backend


STAGE = {"action": "act", "reason": "synthetic initialization", "phase": "initialize",
         "steps": [{"tool": "session.ensure", "arguments": {}},
                   {"tool": "document.new_part", "arguments": {}}]}


def stage_delta(index, decision=None):
    return ({"role": "assistant", "tool_calls": [{"index": 0, "id": f"stage_{index}",
            "type": "function", "function": {"name": "sw_model_stage", "arguments": json.dumps({
                "decision_json": json.dumps(decision or STAGE),
            })}}]}, "tool_calls")


def bridge_request(client, *, request_id="stage-1", decision=None, epoch=None, expires_at=None):
    return {"type": "extension_ui_request", "method": "input", "id": "ui-" + request_id,
            "title": client._stage_bridge_id, "placeholder": json.dumps({
                "request_id": request_id, "epoch": client._stage_epoch if epoch is None else epoch,
                "decision": decision or STAGE,
                "expires_at": time.time() * 1000 + 630_000 if expires_at is None else expires_at,
            })}


def direct_client(tmp_path, monkeypatch, operator):
    client = PiRpcClient(PiRuntime(tmp_path, tmp_path, tmp_path, "fixture"), api_base="local",
                         model="synthetic", api_key="", state_root=tmp_path, session_name="native-stage",
                         operator=operator)
    client.stage_execution_allowed = True
    client._stage_epoch = "one-epoch"
    outputs = []
    monkeypatch.setattr(client, "_write_record", lambda response: outputs.append(json.loads(response["value"])))
    return client, outputs


def test_native_pi_executes_two_stages_and_observes_recovery_in_same_session(tmp_path):
    calls = []
    parent_thread = threading.get_ident()

    def respond(request, index):
        names = {tool["function"]["name"] for tool in request.get("tools", [])}
        assert names <= {"sw_investigate", "sw_model_stage"}
        if index == 1:
            assert "sw_model_stage" in names
            return stage_delta(1)
        if index == 2:
            assert "trial_restored" in json.dumps(request["messages"])
            return stage_delta(2, {**STAGE, "reason": "changed candidate after verified restoration"})
        assert "sw_model_stage" not in names
        return decision_response()

    def operate(envelope):
        calls.append((envelope, threading.get_ident()))
        return {"ok": len(calls) == 2, "status": "passed" if len(calls) == 2 else "trial_restored"}

    with native_client(tmp_path, respond, lambda *args: {"ok": True}) as (client, requests):
        client.operator = operate
        client.stage_execution_allowed = True
        assert json.loads(client.prompt("synthetic CAD stage task"))["action"] == "act"
        assert len(calls) == 2 and all(thread == parent_thread for _, thread in calls)
        assert client._investigation_count == 0 and client._stage_count == 2
        assert len(requests) == 3


def test_native_read_quota_does_not_withdraw_unused_stage_tool(tmp_path):
    calls = []

    def respond(request, index):
        names = {tool["function"]["name"] for tool in request.get("tools", [])}
        if index <= 8:
            return tool_response(index)
        if index == 9:
            assert names == {"sw_model_stage"}
            return stage_delta(9)
        return decision_response()

    with native_client(tmp_path, respond, lambda *args: {"ok": True}) as (client, _):
        client.operator = lambda envelope: calls.append(envelope) or {"ok": True, "status": "passed"}
        client.stage_execution_allowed = True
        client.prompt("read quota then actual stage")
        assert len(calls) == 1 and client._investigation_count == 8


def test_native_prepare_omits_stage_tool_even_when_callback_is_bound(tmp_path):
    calls = []

    def respond(request, _index):
        assert {tool["function"]["name"] for tool in request.get("tools", [])} == {"sw_investigate"}
        return decision_response()

    with native_client(tmp_path, respond, lambda *args: {"ok": True}) as (client, _):
        client.operator = lambda envelope: calls.append(envelope) or {"ok": True}
        client.prompt("prepare-only task")
        assert calls == []


def test_native_model_cannot_exceed_two_stage_attempts(tmp_path):
    calls = []
    with native_client(tmp_path, lambda request, index: stage_delta(index), lambda *args: {"ok": True}) as (client, _):
        client.operator = lambda envelope: calls.append(envelope) or {"ok": True, "status": "passed"}
        client.stage_execution_allowed = True
        with pytest.raises(PiDecisionLoopExceeded, match="阶段额度"):
            client.prompt("model ignores withdrawn stage capability")
        assert len(calls) == 2 and client._investigation_count == 0


def test_native_single_stage_limit_withdraws_tool_after_one_call(tmp_path):
    calls = []

    def respond(request, index):
        if index == 1:
            return stage_delta(1)
        assert "sw_model_stage" not in {tool["function"]["name"] for tool in request.get("tools", [])}
        return decision_response()

    with native_client(tmp_path, respond, lambda *args: {"ok": True}) as (client, _):
        client.operator = lambda envelope: calls.append(envelope) or {"ok": True, "status": "passed"}
        client.stage_execution_allowed = True
        client.stage_attempt_limit = 1
        client.prompt("one stage UI mode")
        assert len(calls) == 1


def test_native_unknown_stage_result_withdraws_writes_and_preserves_read_budget(tmp_path):
    def respond(request, index):
        if index == 1:
            return stage_delta(1)
        assert not request.get("tools")
        assert "unknown" in json.dumps(request["messages"])
        return decision_response()

    with native_client(tmp_path, respond, lambda *args: {"ok": True}) as (client, _):
        client.operator = lambda envelope: {"ok": False, "status": "unknown", "requires_review": True}
        client.stage_execution_allowed = True
        client.prompt("uncertain host stage result")
        assert client._stage_uncertain and client._investigation_count == 0


def test_stage_request_is_idempotent_but_payload_change_and_late_epoch_are_denied(tmp_path, monkeypatch):
    calls = []
    client, outputs = direct_client(tmp_path, monkeypatch, lambda envelope: calls.append(envelope) or {"ok": True})
    original = bridge_request(client)
    client._handle_tool_request(original)
    client._handle_tool_request(original)
    client._handle_tool_request(bridge_request(client, decision={**STAGE, "reason": "changed"}))
    client._handle_tool_request(bridge_request(client, request_id="other", epoch="old-epoch"))
    assert len(calls) == 1
    assert outputs[0] == outputs[1]
    assert outputs[2]["result"]["status"] == outputs[3]["result"]["status"] == "denied"


def test_expired_stage_never_reaches_executor(tmp_path, monkeypatch):
    calls = []
    client, outputs = direct_client(tmp_path, monkeypatch, lambda envelope: calls.append(envelope) or {})
    client._handle_tool_request(bridge_request(client, expires_at=1))
    assert not calls and outputs[-1]["result"]["status"] == "denied"


def test_stage_limit_is_bytes_not_only_character_count(tmp_path, monkeypatch):
    calls = []
    client, outputs = direct_client(tmp_path, monkeypatch, lambda envelope: calls.append(envelope) or {})
    request = bridge_request(client, decision={**STAGE, "reason": "汉" * 11_000})
    # Match the extension's unescaped UTF-8 encoding, not JSON's optional ASCII escapes.
    request["placeholder"] = json.dumps(json.loads(request["placeholder"]), ensure_ascii=False)
    client._handle_tool_request(request)
    assert not calls and outputs[-1]["result"]["status"] == "denied"


def test_execution_exception_is_unknown_and_never_reexecuted(tmp_path, monkeypatch):
    calls = []

    def operate(envelope):
        calls.append(envelope)
        raise RuntimeError("synthetic disconnected after mutation")

    client, outputs = direct_client(tmp_path, monkeypatch, operate)
    original = bridge_request(client)
    client._handle_tool_request(original)
    client._handle_tool_request(original)
    client._handle_tool_request(bridge_request(client, request_id="replacement"))
    assert len(calls) == 1
    assert outputs[0]["result"]["status"] == "unknown"
    assert outputs[0]["result"]["requires_review"] is True
    assert outputs[1]["result"] == outputs[0]["result"]
    assert outputs[2]["result"]["status"] == "denied"


def test_stop_during_stage_discards_late_success_and_preserves_unknown_cache(tmp_path, monkeypatch):
    def operate(_envelope):
        client._aborted.set()
        return {"ok": True, "status": "passed"}

    client, outputs = direct_client(tmp_path, monkeypatch, operate)
    with pytest.raises(PiAgentError, match="禁止重放"):
        client._handle_tool_request(bridge_request(client))
    assert outputs == [] and client._stage_replies["stage-1"]["result"]["status"] == "unknown"


def test_cad_callback_time_extends_only_bounded_stage_wall_clock(tmp_path, monkeypatch):
    request = {"type": "extension_ui_request"}
    events = start_events() + [(8, request)] + end_events(458)
    client, now = simulated_client(tmp_path, monkeypatch, events)
    monkeypatch.setattr(pi_module, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1000))
    original_send = client._send

    def send(name, **kwargs):
        if name == "prompt":
            request.update(bridge_request(client, expires_at=1_630_000))
        return original_send(name, **kwargs)

    def operate(_envelope):
        now[0] += 445
        return {"ok": True, "status": "passed"}

    monkeypatch.setattr(client, "_send", send)
    client.operator = operate
    client.stage_execution_allowed = True
    client.max_turn_seconds = 20
    assert json.loads(client.prompt("synthetic long CAD callback"))["action"] == "inspect"
    assert client._stage_elapsed == 445


def test_stage_capability_does_not_extend_model_silence(tmp_path, monkeypatch):
    client, _ = simulated_client(tmp_path, monkeypatch, start_events() + [(11, stream_delta())])
    client.operator = lambda envelope: {"ok": True}
    client.stage_execution_allowed = True
    with pytest.raises(PiResponseTimeout):
        client.prompt("silent model with stage capability")


def test_stage_exceeding_600_seconds_is_unknown_not_success(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pi_module, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1000))

    def operate(_envelope):
        now[0] = 601
        return {"ok": True, "status": "passed"}

    client, outputs = direct_client(tmp_path, monkeypatch, operate)
    client._handle_tool_request(bridge_request(client, expires_at=1_630_000))
    assert outputs[-1]["result"]["status"] == "unknown" and client._stage_uncertain


def test_backend_stage_requires_run_permission_and_owner_thread(tmp_path):
    service = backend(tmp_path)
    calls, observed = [], []
    service.set_operator(lambda envelope: calls.append(threading.get_ident()) or {"ok": True, "status": "passed"})

    class InvokingPi(FakePi):
        def prompt(self, prompt):
            observed.append(service._operate({"request_id": "one", "decision": STAGE}))
            other = threading.Thread(target=lambda: observed.append(service._operate({"request_id": "two", "decision": STAGE})))
            other.start()
            other.join(3)
            return json.dumps(DECISION)

    service._pi = InvokingPi([])
    service.decide({"agent_stage_execution": {"allowed": False}})
    assert not calls
    service.decide({"agent_stage_execution": {"allowed": True}})
    assert calls == [threading.get_ident()]
    assert [item["ok"] for item in observed] == [False, False, True, False]
    assert service._operate({"request_id": "late", "decision": STAGE})["status"] == "denied"


def test_backend_does_not_fallback_after_stage_entered_executor(tmp_path):
    service = backend(tmp_path)
    calls = []
    service.set_operator(lambda envelope: calls.append(envelope) or {"ok": True, "status": "passed"})

    class DisconnectedPi(FakePi):
        def prompt(self, prompt):
            assert service._operate({"request_id": "once", "decision": STAGE})["ok"]
            raise PiAgentError("synthetic transport failed after execution")

    service._pi = DisconnectedPi([])
    with pytest.raises(TaskBackendUnavailable, match="禁止切换模型重放"):
        service.decide({"agent_stage_execution": {"allowed": True}})
    assert len(calls) == 1 and service.backend_source == "pi"


def test_backend_invalid_and_denied_stages_consume_attempt_budget(tmp_path):
    service = backend(tmp_path)
    calls = []
    results = []
    service.set_operator(lambda envelope: calls.append(envelope) or {"ok": True})

    class InvokingPi(FakePi):
        def prompt(self, prompt):
            for index in range(3):
                results.append(service._operate({"request_id": str(index), "decision": DECISION if index < 2 else STAGE}))
            return json.dumps(DECISION)

    service._pi = InvokingPi([])
    service.decide({"agent_stage_execution": {"allowed": True}})
    assert not calls
    assert results[2]["error_code"] == "StageBudgetExceeded"


def test_backend_final_act_cannot_bypass_native_rejection_budget(tmp_path):
    service = backend(tmp_path)
    calls = []
    service.set_operator(lambda envelope: calls.append(envelope) or {"ok": True})

    class InvalidThenAct(FakePi):
        def prompt(self, prompt):
            service._operate({"request_id": "invalid", "decision": DECISION})
            return json.dumps(STAGE)

    service._pi = InvalidThenAct([])
    with pytest.raises(TaskDecisionInvalid, match="最终 act 不会再次执行"):
        service.decide({"agent_stage_execution": {"allowed": True}})
    assert not calls


def test_pydantic_exposes_explicit_unavailable_native_stage_capability(tmp_path, monkeypatch):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from sw_assistant.agent import task_backend as module

    seen = []

    def respond(messages, info):
        seen.append(messages)
        assert not any(tool.name == "sw_model_stage" for tool in info.function_tools)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, DECISION)])

    monkeypatch.setattr(module, "OpenAIChatModel", lambda *a, **k: FunctionModel(respond))
    service = backend(tmp_path, selected="pydantic")
    service.set_operator(lambda envelope: pytest.fail("Pydantic must not execute a native stage"))
    service.decide({"agent_stage_execution": {"allowed": True}})
    user_text = next(part.content for part in seen[0][-1].parts if part.part_kind == "user-prompt")
    assert json.loads(user_text)["native_stage"]["available"] is False
    assert json.loads(user_text)["context"]["agent_stage_execution"]["allowed"] is False
