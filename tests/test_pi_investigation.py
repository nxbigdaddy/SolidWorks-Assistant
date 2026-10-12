"""Native Pi tool calls use synthetic local endpoints, never user CAD or models."""
from __future__ import annotations

import json
import asyncio
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sw_assistant.agent.pi_client import PiAgentError, PiUnavailableError, discover_pi_runtime
from sw_assistant.agent.task_backend import TaskAgentBackend
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry
from sw_assistant.agent import task_backend as module
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel


def test_native_pi_investigates_twice_in_one_turn_before_deciding(tmp_path):
    try:
        discover_pi_runtime()
    except PiUnavailableError as exc:
        pytest.skip(str(exc))
    requests = []
    observed = []
    parent_thread = threading.get_ident()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            index = len(requests)
            if index < 3:
                tool = "session.capabilities" if index == 1 else "model.references"
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"call_{index}", "type": "function", "function": {
                        "name": "sw_investigate", "arguments": json.dumps({
                            "tool": tool, "arguments_json": "{}" if index == 1 else '{"limit":3}',
                        }),
                    },
                }]}
                reason = "tool_calls"
            else:
                delta = {"role": "assistant", "content": json.dumps({
                    "action": "inspect", "reason": "Candidate FrontA exists; validate the sketch next",
                    "steps": [{"tool": "sketch.inspect", "arguments": {}}],
                })}
                reason = "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part, finish in ((delta, None), ({}, reason)):
                chunk = {"id": f"chatcmpl-{index}", "object": "chat.completion.chunk", "created": 1,
                         "model": "local-investigation", "choices": [{"index": 0, "delta": part,
                                                                         "finish_reason": finish}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = TaskAgentBackend(
        ModelSettings(f"http://127.0.0.1:{server.server_port}/v1", "local-investigation", "fixture-key",
                      timeout_seconds=25), build_tool_registry(), tmp_path,
        session_id="native-investigation", backend="pi",
    )

    def investigate(tool, arguments):
        observed.append((tool, arguments, threading.get_ident()))
        return {"ok": True, "complete": True, "candidates": ["FrontA"],
                "api_key": "never-transmit", "document_path": r"C:\private\cad.sldprt"}

    backend.set_investigator(investigate)
    try:
        result = backend.decide({"task": "construct a generic mounting body", "requirements": []})
        assert result.action == "inspect" and result.steps[0].tool == "sketch.inspect"
        assert observed, backend._pi._diagnostic_summary() if backend._pi else "no Pi client"
        assert observed == [("session.capabilities", {}, parent_thread),
                            ("model.references", {"limit": 3}, parent_thread)]
        assert len(requests) == 3
        assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"sw_investigate"}
        tool_results = [item for item in requests[2]["messages"] if item["role"] == "tool"]
        assert len(tool_results) == 2
        payload = json.dumps(tool_results)
        assert "FrontA" in payload and "never-transmit" not in payload and "private" not in payload
        assert backend._pi is not None and "--no-tools" in backend._pi.command
    finally:
        backend.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_pi_bridge_denies_untrusted_dialog_replays_and_late_requests(tmp_path, monkeypatch):
    from sw_assistant.agent.pi_client import PiRpcClient, PiRuntime

    client = PiRpcClient(PiRuntime(tmp_path, tmp_path, tmp_path, "test"), api_base="local",
                         model="fixture", api_key="", state_root=tmp_path, session_name="test",
                         investigator=lambda tool, args: {"ok": True, "tool": tool})
    sent = []
    monkeypatch.setattr(client, "_write_record", sent.append)
    untrusted = {"type": "extension_ui_request", "id": "unknown", "method": "confirm",
                 "title": "allow shell?"}
    assert client._handle_tool_request(untrusted)
    assert sent[-1]["cancelled"] is True
    request = {"type": "extension_ui_request", "id": "one", "method": "input",
               "title": client._bridge_id,
               "placeholder": json.dumps({"tool": "model.observe", "arguments": {}})}
    calls = []
    client.investigator = lambda tool, args: calls.append(tool) or {"ok": True}
    client._handle_tool_request(request)
    client._handle_tool_request(request)
    assert calls == ["model.observe"]
    assert sent[-1] == sent[-2]
    client.abort()
    with pytest.raises(PiAgentError, match="停止"):
        client._handle_tool_request({**request, "id": "late"})
    assert calls == ["model.observe"]


def test_backend_investigation_bounds_and_redacts_results(tmp_path):
    backend = TaskAgentBackend(ModelSettings("http://localhost/v1", "fake", "fixture-key"),
                               build_tool_registry(), tmp_path, session_id="bounds")
    calls = []
    backend.set_investigator(lambda tool, args: calls.append(tool) or {
        "ok": True, "api_key": "secret", "message": "fixture-key",
        "path": r"C:\private\model.sldprt",
    })
    assert not backend._investigate("feature.extrude", {})["ok"]
    for _ in range(8):
        result = backend._investigate("session.capabilities", {})
        assert result["ok"] and "fixture-key" not in json.dumps(result)
        assert "private" not in json.dumps(result)
    with pytest.raises(module.TaskDecisionInvalid, match="调查预算"):
        backend._investigate("session.capabilities", {})
    assert len(calls) == 8
    backend.close()


def test_builtin_investigates_on_operation_thread_and_trims_whole_turns(tmp_path, monkeypatch):
    requests, observed = [], []
    parent = threading.get_ident()

    def model(messages, info):
        requests.append(list(messages))
        if len(requests) % 2:
            return ModelResponse(parts=[ToolCallPart("sw_investigate", {
                "tool": "session.capabilities", "arguments_json": "{}",
            })])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "action": "inspect", "reason": "capability observation received",
        })])

    monkeypatch.setattr(module, "OpenAIChatModel", lambda *a, **k: FunctionModel(model))
    backend = TaskAgentBackend(ModelSettings("http://localhost/v1", "fake", "fixture-key"),
                               build_tool_registry(), tmp_path, session_id="fallback", backend="pydantic")
    backend.set_investigator(lambda tool, args: observed.append(threading.get_ident()) or {
        "ok": True, "complete": True, "tools": ["feature.extrude"],
    })
    try:
        assert asyncio.run(backend._run_pydantic("first user turn")).action == "inspect"
        monkeypatch.setattr(module, "MAX_AGENT_CONTEXT_BYTES", 50)
        assert asyncio.run(backend._run_pydantic("second user turn")).action == "inspect"
        assert observed == [parent, parent]
        parts = requests[2][0].parts
        assert any(isinstance(part, UserPromptPart) for part in parts)
        assert not any(isinstance(part, ToolReturnPart) for part in parts)
        assert "first user turn" not in str(requests[2])
    finally:
        backend.close()


def test_both_backends_reject_nonfinite_investigation_results(tmp_path):
    backend = TaskAgentBackend(ModelSettings("http://localhost/v1", "fake", "fixture-key"),
                               build_tool_registry(), tmp_path, session_id="nonfinite")
    backend.set_investigator(lambda tool, args: {"ok": True, "volume": float("nan")})
    assert backend._investigate("model.inspect", {})["ok"] is False
    backend.close()


def test_pi_restart_stdout_queue_isolation_and_stale_settled_cannot_finish(tmp_path, monkeypatch):
    from sw_assistant.agent.pi_client import PiRpcClient, PiRuntime

    client = PiRpcClient(PiRuntime(tmp_path, tmp_path, tmp_path, "test"), api_base="local",
                         model="fake", api_key="", state_root=tmp_path, session_name="test")
    old = queue.Queue()
    client._decode_stdout_record(b'{"type":"agent_settled"}', old)
    assert old.qsize() == 1 and client._messages.empty()
    commands = []
    events = iter([
        {"type": "agent_settled"},
        {"type": "response", "id": "request", "success": True},
    ])
    monkeypatch.setattr(client, "start", lambda: None)
    monkeypatch.setattr(client, "_send", lambda name, **kw: commands.append(name) or "request")

    def receive(_deadline):
        try:
            return next(events)
        except StopIteration:
            raise PiAgentError("no new agent turn arrived")

    monkeypatch.setattr(client, "_receive", receive)
    with pytest.raises(PiAgentError, match="no new"):
        client.prompt("new prompt")
    assert commands == ["prompt"]
