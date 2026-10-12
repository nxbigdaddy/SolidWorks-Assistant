"""Local fake clocks/events only: no CAD, model endpoints, or real Pi process."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sw_assistant.agent import pi_client as pi_module
from sw_assistant.agent.pi_client import PiCompactionTimeout, PiResponseTimeout, PiTurnBudgetExceeded
from sw_assistant.agent.task_backend import TaskBackendUnavailable
from test_agent_response_budget import end_events, simulated_client, start_events, stream_delta
from test_task_backend import FakePi, backend
from test_task_runtime import make_controller


def compact(at):
    return at, {"type": "compaction_start"}


def compacted(at):
    return at, {"type": "compaction_end"}


def test_completed_response_can_finish_compaction_beyond_response_idle(tmp_path, monkeypatch):
    events = start_events() + [(7, stream_delta()), (7, {"type": "agent_end"}), compact(8), compacted(25)] + end_events(26)
    client, now = simulated_client(tmp_path, monkeypatch, events)
    sends, observed = [], []
    monkeypatch.setattr(client, "_send", lambda name, **kwargs: sends.append((name, now[0])) or name)
    client.on_event = lambda kind, payload: observed.append(kind)
    assert json.loads(client.prompt("synthetic task"))["action"] == "inspect"
    assert sends == [("prompt", 0), ("get_last_assistant_text", 26)]
    assert "compaction_start" in observed and "compaction_end" in observed
    assert client.timeout_seconds == 10


def test_compaction_before_prompt_ack_is_still_bounded_maintenance(tmp_path, monkeypatch):
    events = [compact(0), (20, {"type": "response", "id": "prompt", "success": True}),
              (20, {"type": "agent_start"}), compacted(21)] + end_events(22)
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    assert json.loads(client.prompt("synthetic task"))["action"] == "inspect"
    assert client._compaction_deadline == 300


@pytest.mark.parametrize("noise", [{"type": "compaction_start"}, {"type": "heartbeat"}, stream_delta()])
def test_compaction_window_is_fixed_despite_repeated_start_or_activity(tmp_path, monkeypatch, noise):
    monkeypatch.setattr(pi_module, "COMPACTION_TIMEOUT_SECONDS", 30.0)
    events = start_events() + [compact(8), (15, noise), (25, noise), (35, noise), (39, noise)]
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    with pytest.raises(PiCompactionTimeout, match="固定预算"):
        client.prompt("synthetic task")
    assert client._compaction_deadline == 38
    assert closed == [True]


def test_second_compaction_in_same_prompt_does_not_get_a_new_window(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_module, "COMPACTION_TIMEOUT_SECONDS", 30.0)
    events = start_events() + [compact(1), compacted(5), compact(9), compacted(35)]
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    with pytest.raises(PiCompactionTimeout):
        client.prompt("synthetic task")
    assert client._compaction_deadline == 31


@pytest.mark.parametrize("limit", ["turn", "decision"])
def test_compaction_respects_shorter_hard_deadline_without_fallback_classification(tmp_path, monkeypatch, limit):
    client, _ = simulated_client(tmp_path, monkeypatch, start_events() + [compact(8), compacted(25)])
    if limit == "turn":
        client.max_turn_seconds = 20
    else:
        original_check = client._check_tool_loop

        def deadline(item):
            original_check(item)
            if item.get("type") == "compaction_start":
                client._decision_deadline = 20
                client._decision_only = True
        monkeypatch.setattr(client, "_check_tool_loop", deadline)
    with pytest.raises(PiCompactionTimeout, match="本轮剩余") as raised:
        client.prompt("synthetic task")
    assert isinstance(raised.value, PiTurnBudgetExceeded)
    assert not getattr(raised.value, "retryable_decision", False)


def test_compaction_end_restores_original_response_idle_timeout(tmp_path, monkeypatch):
    events = start_events() + [compact(8), compacted(25), (36, {"type": "heartbeat"})]
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    with pytest.raises(PiResponseTimeout):
        client.prompt("synthetic task")
    assert not client._compacting


def test_next_explicit_prompt_has_a_fresh_compaction_window(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_module, "COMPACTION_TIMEOUT_SECONDS", 30.0)
    events = (start_events() + [compact(5), compacted(19)] + end_events(20)
              + start_events() + [compact(25), compacted(48)] + end_events(49))
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    assert client.prompt("first explicit task")
    assert client.prompt("second explicit task")
    assert client._compaction_deadline == 55


@pytest.mark.parametrize("tool_event", [{"type": "tool_execution_start", "toolName": "sw_model_stage"},
                                     {"type": "extension_ui_request", "method": "input", "id": "late"}])
def test_no_new_tool_dispatch_during_compaction(tmp_path, monkeypatch, tool_event):
    client, _ = simulated_client(tmp_path, monkeypatch, start_events() + [compact(5), (6, tool_event)])
    callbacks = []
    client.investigator = lambda *args: callbacks.append(args)
    with pytest.raises(PiTurnBudgetExceeded, match="未执行或重放工具"):
        client.prompt("synthetic task")
    assert callbacks == []


def test_auto_backend_never_downgrades_or_replays_after_compaction_expiry(tmp_path, monkeypatch):
    service = backend(tmp_path)
    client = FakePi([PiCompactionTimeout("Pi 上下文整理达到固定预算")])
    service._pi = client
    alternatives = []

    async def forbidden(prompt):
        alternatives.append(prompt)
        raise AssertionError("compaction is not a reason to rerun on another backend")

    monkeypatch.setattr(service, "_run_pydantic", forbidden)
    try:
        with pytest.raises(TaskBackendUnavailable, match="上下文整理"):
            service.decide({})
        assert service.backend_source == "pi"
        assert len(client.prompts) == 1 and alternatives == []
        assert '"event":"fallback"' not in (service.state_root / "decisions.jsonl").read_text(encoding="utf-8")
    finally:
        service.close()


def test_compaction_progress_is_public_and_survives_snapshot_then_stop(tmp_path):
    controller = make_controller(tmp_path)
    task_service = controller.continuous
    task_service.runner = SimpleNamespace(snapshot=lambda: {"id": "one-task", "status": "running"}, stop=lambda: None)
    task_service._busy = True
    inference = backend(tmp_path / "inference")
    inference.on_event = lambda name, message, details: task_service._event(name, {"message": message, **details})
    try:
        inference._on_pi_event("compaction_start", {"private_summary": "do not expose raw context"})
        shown = task_service.snapshot()["agent_progress_message"]
        assert "整理会话上下文" in shown and "300" in shown
        assert "收到" not in shown and "private" not in shown
        inference._on_pi_event("compaction_end", {})
        assert "整理结束" in task_service.snapshot()["agent_progress_message"]
        task_service.stop()
        inference._on_pi_event("compaction_start", {})
        assert task_service.snapshot()["agent_progress_message"] == ""
    finally:
        task_service._busy = False
        inference.close()
        controller.shutdown()
