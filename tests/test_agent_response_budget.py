"""No real model/CAD: response progress must not consume a whole-turn deadline."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sw_assistant.agent import pi_client as pi_module
from sw_assistant.agent import task_backend as backend_module
from sw_assistant.agent.pi_client import (
    PiResponseTimeout, PiRpcClient, PiRuntime, PiTurnBudgetExceeded, PiUnavailableError, discover_pi_runtime,
)
from sw_assistant.agent.task_backend import TaskBackendUnavailable
from sw_assistant.agent.task_models import TaskDecision
from test_task_backend import DECISION, FakePi, backend


def simulated_client(tmp_path, monkeypatch, events, *, investigator=None):
    now = [0.0]
    monkeypatch.setattr(pi_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    client = PiRpcClient(PiRuntime(tmp_path, tmp_path, tmp_path, "fixture"), api_base="local",
                         model="synthetic", api_key="", state_root=tmp_path, session_name="budget",
                         timeout_seconds=10, investigator=investigator)
    pending = iter(events)
    monkeypatch.setattr(client, "start", lambda: None)
    monkeypatch.setattr(client, "_send", lambda name, **kw: name)
    monkeypatch.setattr(client, "_write_record", lambda value: None)

    def receive(deadline):
        timestamp, event = next(pending)
        now[0] = max(now[0], timestamp)
        if now[0] >= deadline:
            raise PiResponseTimeout("inactivity")
        return event

    monkeypatch.setattr(client, "_receive", receive)
    return client, now


def stream_delta(text="working"):
    return {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}}


def start_events():
    return [(0, {"type": "response", "id": "prompt", "success": True}),
            (0, {"type": "agent_start"})]


def end_events(timestamp):
    return [(timestamp, {"type": "agent_settled"}),
            (timestamp, {"type": "response", "id": "get_last_assistant_text", "success": True,
                         "data": {"text": json.dumps(DECISION)}})]


def test_active_response_can_exceed_single_inactivity_timeout(tmp_path, monkeypatch):
    events = start_events() + [(9, stream_delta()), (18, stream_delta())] + end_events(25)
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    assert json.loads(client.prompt("synthetic task"))["action"] == "inspect"


@pytest.mark.parametrize("noise", [
    {"type": "heartbeat"}, stream_delta(""), stream_delta(" \r\n\t"), {"type": "agent_start"},
    {"type": "response", "id": "prompt", "success": True},
])
def test_housekeeping_cannot_reset_inactivity(tmp_path, monkeypatch, noise):
    client, _ = simulated_client(tmp_path, monkeypatch, start_events() + [(9, noise), (11, noise)])
    with pytest.raises(PiResponseTimeout):
        client.prompt("synthetic task")


def test_continuous_tokens_still_have_independent_hard_limit(tmp_path, monkeypatch):
    client, _ = simulated_client(tmp_path, monkeypatch,
                                start_events() + [(9, stream_delta()), (18, stream_delta()), (25, stream_delta())])
    client.max_turn_seconds = 20
    with pytest.raises(PiTurnBudgetExceeded, match="20 秒活动预算"):
        client.prompt("synthetic task")


def test_valid_host_tool_time_is_not_model_inactivity(tmp_path, monkeypatch):
    request = {"type": "extension_ui_request", "id": "investigate", "method": "input",
               "placeholder": json.dumps({"tool": "model.observe", "arguments": {}})}
    client, now = simulated_client(tmp_path, monkeypatch, start_events() + [(8, request)] + end_events(58))
    request["title"] = client._bridge_id

    def investigate(*_args):
        now[0] += 45
        return {"ok": True}

    client.investigator = investigate
    assert json.loads(client.prompt("synthetic task"))["action"] == "inspect"


def test_private_stream_content_never_becomes_public_progress(tmp_path, monkeypatch):
    client, now = simulated_client(tmp_path, monkeypatch, [])
    seen = []
    client.on_event = lambda name, payload: seen.append((name, payload))
    now[0] = 5
    client._emit_event({"type": "message_update", "assistantMessageEvent": {
        "type": "thinking_delta", "delta": "private reasoning text",
    }})
    assert seen == [("response_progress", {})]


def test_fallback_explains_cause_without_leaking_key(tmp_path, monkeypatch):
    from pydantic_ai.models.test import TestModel as ModelStub

    service = backend(tmp_path)
    seen = []
    service.on_event = lambda stage, message, data: seen.append((stage, data))
    service._pi = FakePi([PiResponseTimeout("idle sensitive-key-123")])
    monkeypatch.setattr(backend_module, "OpenAIChatModel", lambda *a, **k: ModelStub(
        call_tools=[], custom_output_args=DECISION,
    ))
    try:
        service.decide({})
        event = next(data for stage, data in seen if stage == "agent_fallback")
        assert event["engine_source"] == "pydantic"
        assert "idle" in event["reason"] and "sensitive-key-123" not in json.dumps(event)
    finally:
        service.close()


def test_turn_budget_is_not_treated_as_reason_to_switch_engines(tmp_path):
    service = backend(tmp_path)
    service._pi = FakePi([PiTurnBudgetExceeded("active turn budget")])
    try:
        with pytest.raises(TaskBackendUnavailable, match="active turn budget"):
            service.decide({})
        assert service.backend_source == "pi"
    finally:
        service.close()


def test_builtin_whole_turn_budget_is_distinct_from_response_wait(tmp_path, monkeypatch):
    service = backend(tmp_path, selected="pydantic")
    seen = []
    original = backend_module.asyncio.timeout

    def waiting(timeout):
        seen.append(timeout)
        return original(timeout)

    async def answer(_prompt):
        return TaskDecision.model_validate(DECISION)

    monkeypatch.setattr(backend_module.asyncio, "timeout", waiting)
    monkeypatch.setattr(service, "_run_pydantic", answer)
    try:
        service.decide({})
        assert seen == [900]
    finally:
        service.close()


def test_provider_timeout_is_not_reported_as_whole_turn_expiry(tmp_path, monkeypatch):
    service = backend(tmp_path, selected="pydantic")

    async def failed(_prompt):
        raise TimeoutError("provider response wait expired")

    monkeypatch.setattr(service, "_run_pydantic", failed)
    try:
        with pytest.raises(TaskBackendUnavailable, match="provider response wait expired"):
            service.decide({})
    finally:
        service.close()


def test_real_pi_stream_progress_survives_total_response_longer_than_idle_limit(tmp_path):
    try:
        runtime = discover_pi_runtime()
    except PiUnavailableError as exc:
        pytest.skip(str(exc))
    request_count = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            request_count.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            content = json.dumps(DECISION)
            split = len(content) // 3
            chunks = [content[:split], content[split:2 * split], content[2 * split:]]
            for index, part in enumerate(chunks):
                if index:
                    time.sleep(2)
                data = {"id": "local-budget", "object": "chat.completion.chunk", "created": 1,
                        "model": "synthetic", "choices": [{"index": 0, "delta": {
                            "role": "assistant", "content": part,
                        }, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(data).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = PiRpcClient(runtime, api_base=f"http://127.0.0.1:{server.server_port}/v1", model="synthetic",
                         api_key="local-fixture", state_root=tmp_path, session_name="stream-budget",
                         timeout_seconds=3, system_prompt="Return the synthetic JSON fixture.")
    try:
        # Four seconds of response content exceeds a three-second idle limit,
        # but no individual gap does. This uses the real installed Pi protocol.
        assert json.loads(client.prompt("synthetic local task"))["action"] == "inspect"
        assert len(request_count) == 1
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
