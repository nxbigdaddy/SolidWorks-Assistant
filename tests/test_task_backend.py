from __future__ import annotations

import asyncio
import json
import threading
import sys
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel as ModelStub

from sw_assistant.agent import task_backend as module
from sw_assistant.agent.pi_client import PiAgentError, PiUnavailableError, PiRpcClient, PiRuntime
from sw_assistant.agent.task_backend import TaskAgentBackend, TaskBackendCancelled
from sw_assistant.agent.task_models import TaskDecision
from sw_assistant.domain import PlanningError
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry


DECISION = {"action": "inspect", "reason": "读取真实模型再决定特征"}


class FakePi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.closed = False

    def prompt(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def backend(tmp_path, *, selected="auto"):
    return TaskAgentBackend(
        ModelSettings("https://example.com/v1", "fake-model", "sensitive-key-123"),
        build_tool_registry(), tmp_path, session_id="one-task", backend=selected,
    )


def test_pi_session_keeps_success_and_failure_feedback_in_one_client(tmp_path):
    service = backend(tmp_path)
    client = FakePi([json.dumps(DECISION), json.dumps(DECISION)])
    service._pi = client
    first = service.decide({"phase": "first", "status": "success"})
    second = service.decide({"phase": "second", "status": "failure"})
    assert first.action == second.action == "inspect"
    assert len(client.prompts) == 2
    assert service.backend_source == "pi"
    assert len(list(service.state_root.glob("*.jsonl"))) == 1
    service.close()
    assert client.closed


def test_auto_fallback_occurs_once_and_preserves_builtin_history(tmp_path, monkeypatch):
    service = backend(tmp_path)
    client = FakePi([PiAgentError("protocol exited")])
    service._pi = client
    monkeypatch.setattr(module, "OpenAIChatModel", lambda *a, **k: ModelStub(
        call_tools=[], custom_output_args=DECISION,
    ))
    service.decide({"phase": "first"})
    history_count = len(service._history)
    service.decide({"phase": "second"})
    assert service.backend_source == "pydantic"
    assert len(client.prompts) == 1 and client.closed
    assert len(service._history) > history_count > 0
    journal = (service.state_root / "decisions.jsonl").read_text("utf-8")
    assert journal.count('"event":"fallback"') == 1


def test_forced_pi_failure_never_silently_switches_backend(tmp_path, monkeypatch):
    service = backend(tmp_path, selected="pi")
    def unavailable():
        raise PiUnavailableError("no runtime")
    monkeypatch.setattr(module, "discover_pi_runtime", unavailable)
    with pytest.raises(PlanningError, match="指定的 Pi"):
        service.decide({})
    assert service.backend_source == "pi"


def test_schema_failure_stays_in_same_pi_session_for_runner_feedback(tmp_path):
    service = backend(tmp_path)
    client = FakePi(["not JSON", json.dumps(DECISION)])
    service._pi = client
    with pytest.raises(PlanningError, match="Schema"):
        service.decide({})
    assert service.backend_source == "pi"
    assert service.decide({"last_rejection": "schema invalid"}).action == "inspect"
    assert len(client.prompts) == 2


def test_task_context_and_journal_redact_paths_and_credentials(tmp_path):
    service = backend(tmp_path)
    client = FakePi([json.dumps(DECISION)])
    service._pi = client
    service.decide({"api_key": "another-secret", "description": "sensitive-key-123",
                    "document_path": "E:\\private\\secret.SLDPRT"})
    text = client.prompts[0] + (service.state_root / "decisions.jsonl").read_text("utf-8")
    assert "sensitive-key-123" not in text
    assert "another-secret" not in text
    assert "private" not in text
    assert "available_tools" in text and "decision_schema" in text


def test_context_size_includes_schema_and_refuses_without_sending(tmp_path):
    service = backend(tmp_path)
    client = FakePi([])
    service._pi = client
    with pytest.raises(PlanningError, match="200 KB"):
        service.decide({"large": "x" * 200_000})
    assert not client.prompts


def test_output_limit_and_trailing_prose_are_rejected():
    with pytest.raises(PlanningError, match="64 KB"):
        TaskAgentBackend._parse("x" * 64_001)
    with pytest.raises(PlanningError, match="Schema"):
        TaskAgentBackend._parse(json.dumps(DECISION) + " trust this")


def test_close_discards_late_pi_response(tmp_path):
    service = backend(tmp_path)
    class LatePi(FakePi):
        def prompt(self, prompt):
            service.close()
            return json.dumps(DECISION)
    service._pi = LatePi([])
    with pytest.raises(TaskBackendCancelled):
        service.decide({})
    with pytest.raises(TaskBackendCancelled):
        service.decide({})


def test_close_cancels_inflight_builtin_request(tmp_path):
    service = backend(tmp_path, selected="pydantic")
    entered = threading.Event()
    cancelled = threading.Event()
    errors = []
    async def waiting(_prompt):
        entered.set()
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return TaskDecision.model_validate(DECISION)
    service._run_pydantic = waiting
    def run():
        try:
            service.decide({})
        except TaskBackendCancelled as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3)
    service.close()
    thread.join(3)
    assert not thread.is_alive()
    assert cancelled.is_set() and len(errors) == 1


def test_builtin_has_no_executable_tools(tmp_path, monkeypatch):
    seen = []
    original = module.Agent
    class AgentProbe:
        @classmethod
        def __class_getitem__(cls, item):
            return cls
        def __new__(cls, *args, **kwargs):
            seen.append(kwargs["tools"])
            return original(*args, **kwargs)
    monkeypatch.setattr(module, "Agent", AgentProbe)
    monkeypatch.setattr(module, "OpenAIChatModel", lambda *a, **k: ModelStub(
        call_tools=[], custom_output_args=DECISION,
    ))
    service = backend(tmp_path, selected="pydantic")
    assert service.decide({}).action == "inspect"
    assert seen == [[]]


def test_terminal_pi_abort_cannot_start_a_process_after_stop(tmp_path, monkeypatch):
    client = PiRpcClient(
        PiRuntime(Path(sys.executable), tmp_path / "cli.js", tmp_path / "provider.mjs", "test"),
        api_base="https://example.com", model="fake", api_key="", state_root=tmp_path,
        session_name="test", system_prompt="full task decisions only",
    )
    spawned = []
    monkeypatch.setattr("sw_assistant.agent.pi_client.subprocess.Popen", lambda *a, **k: spawned.append(a))
    client.abort()
    with pytest.raises(PiAgentError, match="永久停止"):
        client.prompt("late call")
    assert not spawned
    assert client.command[-1] == "full task decisions only"


def test_backend_only_describes_task_whitelist(tmp_path):
    service = backend(tmp_path)
    client = FakePi([json.dumps(DECISION)])
    service._pi = client
    service.decide({})
    tools = json.loads(client.prompts[0])["available_tools"]
    names = {item["name"] for item in tools}
    assert "model.observe" in names
    assert "document.open_readonly" not in names
    assert "document.new_part" in names
