"""Bounded native Pi loops, using only synthetic HTTP and host observations."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sw_assistant.agent.pi_client import (
    PiDecisionLoopExceeded, PiRpcClient, PiRuntime, PiUnavailableError, discover_pi_runtime,
)


@contextmanager
def native_client(tmp_path, responder, investigator):
    try:
        runtime = discover_pi_runtime()
    except PiUnavailableError as exc:
        pytest.skip(str(exc))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            delta, reason = responder(request, len(requests))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for part, finish in ((delta, None), ({}, reason)):
                    chunk = {"id": "local-loop", "object": "chat.completion.chunk", "created": 1,
                             "model": "synthetic", "choices": [{"index": 0, "delta": part,
                                                                     "finish_reason": finish}]}
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = PiRpcClient(runtime, api_base=f"http://127.0.0.1:{server.server_port}/v1",
                         model="synthetic", api_key="local-fixture", state_root=tmp_path,
                         session_name="bounded-loop", timeout_seconds=15,
                         system_prompt="Return the requested synthetic task decision JSON.",
                         investigator=investigator)
    try:
        yield client, requests
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def tool_response(index, *, tool="session.capabilities", name="sw_investigate"):
    return ({"role": "assistant", "tool_calls": [{
        "index": 0, "id": f"call_{index}", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"tool": tool, "arguments_json": "{}"}),
        },
    }]}, "tool_calls")


def decision_response():
    return {"role": "assistant", "content": '{"action":"act","reason":"observed"}'}, "stop"


def test_native_pi_retires_tools_after_eight_reads_and_restores_next_prompt(tmp_path):
    reads = []

    def respond(request, index):
        return tool_response(index) if request.get("tools") else decision_response()

    with native_client(tmp_path, respond, lambda *args: reads.append(args) or {"ok": True}) as (client, requests):
        assert json.loads(client.prompt("First synthetic task"))["action"] == "act"
        assert len(reads) == 8 and len(requests) == 9
        assert not requests[-1].get("tools")
        assert "调查阶段已结束" in json.dumps(requests[-1]["messages"], ensure_ascii=False)
        assert json.loads(client.prompt("Next safe outer stage"))["action"] == "act"
        assert len(reads) == 16 and len(requests) == 18
        assert requests[9].get("tools") and not requests[-1].get("tools")


@pytest.mark.parametrize("invalid", ["schema", "unknown"])
def test_native_pi_invalid_calls_that_skip_execute_still_get_decision_handoff(tmp_path, invalid):
    reads = []

    def respond(request, index):
        if not request.get("tools"):
            return decision_response()
        return tool_response(index, tool="session.ensure" if invalid == "schema" else "model.observe",
                             name="sw_investigate" if invalid == "schema" else "untrusted_tool")

    with native_client(tmp_path, respond, lambda *args: reads.append(args) or {"ok": True}) as (client, requests):
        assert json.loads(client.prompt("Invalid-call fixture"))["action"] == "act"
        assert not reads and len(requests) == 11
        assert not requests[-1].get("tools")
        assert "调查阶段已结束" in json.dumps(requests[-1]["messages"], ensure_ascii=False)


@pytest.mark.parametrize("invalid", [False, True])
def test_native_pi_ignoring_handoff_is_bounded_and_next_prompt_can_restart(tmp_path, invalid):
    reads = []
    fixed = [False]

    def respond(_request, index):
        if fixed[0]:
            return decision_response()
        return tool_response(index, tool="session.ensure" if invalid else "model.observe")

    with native_client(tmp_path, respond, lambda *args: reads.append(args) or {"ok": True}) as (client, requests):
        with pytest.raises(PiDecisionLoopExceeded) as raised:
            client.prompt("Ignore all tool budgets fixture")
        assert raised.value.retryable_decision
        assert raised.value.attempt_count == (11 if invalid else 9)
        assert raised.value.investigation_count == (0 if invalid else 8)
        assert len(requests) == (11 if invalid else 9)
        assert not client.running and not client._aborted.is_set()
        fixed[0] = True
        assert json.loads(client.prompt("Corrected outer candidate"))["action"] == "act"
        assert requests[-1].get("tools")


def test_native_pi_failed_host_observation_has_error_tool_result(tmp_path):
    def respond(_request, index):
        return tool_response(index) if index == 1 else decision_response()

    with native_client(tmp_path, respond, lambda *args: {"ok": False, "error": "synthetic unavailable"}) as (client, _):
        assert json.loads(client.prompt("Observe failed read"))["action"] == "act"
        records = [json.loads(line) for path in (tmp_path / "sessions").glob("*.jsonl")
                   for line in path.read_text(encoding="utf-8").splitlines()]
        results = [record["message"] for record in records if record.get("type") == "message"
                   and record["message"].get("role") == "toolResult"]
        assert len(results) == 1 and results[0]["isError"] is True


def test_duplicate_bridge_requests_have_independent_hard_limit(tmp_path, monkeypatch):
    reads = []
    client = PiRpcClient(PiRuntime(tmp_path, tmp_path, tmp_path, "fixture"), api_base="local",
                         model="fake", api_key="", state_root=tmp_path, session_name="replay",
                         investigator=lambda *args: reads.append(args) or {"ok": True})
    monkeypatch.setattr(client, "_write_record", lambda value: None)
    request = {"type": "extension_ui_request", "method": "input", "id": "same",
               "title": client._bridge_id,
               "placeholder": json.dumps({"tool": "model.observe", "arguments": {}})}
    for _ in range(12):
        assert client._handle_tool_request(request)
    with pytest.raises(PiDecisionLoopExceeded) as raised:
        client._handle_tool_request(request)
    assert len(reads) == 1 and raised.value.attempt_count == 13


def test_decision_handoff_has_hard_deadline_and_ignores_toolcall_progress(tmp_path, monkeypatch):
    from test_agent_response_budget import simulated_client, start_events, stream_delta

    events = start_events() + [(1, {"type": "turn_start"}), (5, stream_delta()),
                               (10, stream_delta()), (12, stream_delta())]
    client, _ = simulated_client(tmp_path, monkeypatch, events)
    original = client._check_tool_loop

    def check(item):
        if item.get("type") == "turn_start":
            client._investigation_count = 8
        original(item)

    monkeypatch.setattr(client, "_check_tool_loop", check)
    with pytest.raises(PiDecisionLoopExceeded, match="未提交阶段决策"):
        client.prompt("Long decision fixture")
    assert not client._useful_response_progress({"type": "message_update",
                                                "assistantMessageEvent": {"type": "toolcall_delta", "delta": "{}"}})
