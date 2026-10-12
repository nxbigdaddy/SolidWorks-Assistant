from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sw_assistant.agent import (
    PiRpcClient,
    PiRuntime,
    PiSupervisorService,
    SupervisorAction,
    discover_pi_runtime,
)
from sw_assistant.domain import ToolCall
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry


def _context() -> dict:
    return {
        "original_task": "create a body",
        "completed_steps": [
            ToolCall("session.ensure", {}).to_dict(),
            ToolCall("document.new_part", {}).to_dict(),
            ToolCall("sketch.begin", {"plane": "front"}).to_dict(),
        ],
        "failed_step": ToolCall(
            "sketch.polygon", {"radius_mm": 2, "sides": 6}
        ).to_dict(),
        "recovery_assessment": {
            "category": "geometry",
            "model_changed": False,
            "state_known": True,
            "safe_retry": False,
            "ai_repair_recommended": True,
        },
        "previous_failed_attempts": [],
    }


def _replacement(radius: float = 3) -> str:
    return json.dumps(
        {
            "action": "replace_remaining",
            "reason": "use a different validated closed profile",
            "replacement_steps": [
                {
                    "tool": "sketch.circle",
                    "arguments": {"radius_mm": radius},
                    "description": "closed profile",
                },
                {
                    "tool": "feature.extrude",
                    "arguments": {"depth_mm": 10},
                    "description": "create body",
                },
            ],
            "expected_postconditions": ["one body exists"],
            "confidence": 0.9,
        }
    )


class FakePiClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []
        self.closed = False

    def prompt(self, message: str) -> str:
        self.prompts.append(message)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _service(tmp_path, client: FakePiClient, rounds: int = 6):
    return PiSupervisorService(
        ModelSettings("https://example.com/v1", "test-model", "secret"),
        build_tool_registry(),
        tmp_path,
        session_name="test",
        max_rounds=rounds,
        client=client,
    )


def test_pi_revises_invalid_json_in_same_session(tmp_path):
    client = FakePiClient(["not json", f"```json\n{_replacement()}\n```"])
    service = _service(tmp_path, client)

    outcome = service.supervise(_context())

    assert outcome.plan is not None
    assert [step.tool for step in outcome.plan.steps] == [
        "sketch.circle",
        "feature.extrude",
    ]
    assert len(client.prompts) == 2
    assert "本地安全策略拒绝" in client.prompts[1]
    assert service.rounds_used == 2


def test_pi_revises_policy_rejected_repeated_call(tmp_path):
    repeated = json.dumps(
        {
            "action": "replace_remaining",
            "reason": "retry the same call",
            "replacement_steps": [
                {
                    "tool": "sketch.polygon",
                    "arguments": {"radius_mm": 2, "sides": 6},
                    "description": "repeat",
                }
            ],
            "expected_postconditions": ["closed sketch"],
            "confidence": 0.8,
        }
    )
    client = FakePiClient([repeated, _replacement()])
    service = _service(tmp_path, client)

    outcome = service.supervise(_context())

    assert outcome.plan is not None
    assert "重复" in client.prompts[1]
    assert service.rounds_used == 2


def test_pi_service_preserves_conversation_and_budget_between_failures(tmp_path):
    client = FakePiClient([_replacement(3), _replacement(4)])
    service = _service(tmp_path, client)

    first = service.supervise(_context())
    second = service.supervise(_context())

    assert first.plan is not None and second.plan is not None
    assert len(client.prompts) == 2
    assert service.rounds_used == 2


def test_pi_budget_exhaustion_returns_review_without_executable_plan(tmp_path):
    client = FakePiClient(["invalid", "still invalid"])
    service = _service(tmp_path, client, rounds=2)

    outcome = service.supervise(_context())

    assert outcome.decision.action == SupervisorAction.REQUEST_REVIEW
    assert outcome.plan is None
    assert service.rounds_remaining == 0


def test_pi_runtime_discovery_honors_explicit_isolated_paths(tmp_path):
    node = tmp_path / "node.exe"
    cli = tmp_path / "cli.js"
    extension = tmp_path / "provider.mjs"
    for path in (node, cli, extension):
        path.write_text("test", "utf-8")

    runtime = discover_pi_runtime(
        extension,
        environ={
            "SW_ASSISTANT_NODE": str(node),
            "SW_ASSISTANT_PI_CLI": str(cli),
        },
    )

    assert runtime.source == "configured"
    assert runtime.node == node
    assert runtime.cli == cli


def test_pi_command_never_contains_api_key_and_disables_all_tools(tmp_path):
    extension = tmp_path / "provider.mjs"
    cli = tmp_path / "cli.js"
    extension.write_text("test", "utf-8")
    cli.write_text("test", "utf-8")
    runtime = PiRuntime(Path(sys.executable), cli, extension, "test")
    client = PiRpcClient(
        runtime,
        api_base="https://example.com/v1",
        model="test-model",
        api_key="top-secret-key",
        state_root=tmp_path / "state",
        session_name="test",
    )

    assert "top-secret-key" not in " ".join(client.command)
    assert "--no-tools" in client.command
    assert "--no-extensions" in client.command
    assert "--no-context-files" in client.command
    assert client.child_environment["SW_ASSISTANT_PI_API_KEY"] == "top-secret-key"


def test_pi_rpc_client_obeys_jsonl_protocol(tmp_path):
    script = tmp_path / "fake_pi.py"
    script.write_text(
        """
import json
import sys
for line in sys.stdin:
    command = json.loads(line)
    if command["type"] == "prompt":
        print(json.dumps({"id": command["id"], "type": "response", "command": "prompt", "success": True}), flush=True)
        print(json.dumps({"type": "agent_start"}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
    elif command["type"] == "get_last_assistant_text":
        print(json.dumps({"id": command["id"], "type": "response", "command": "get_last_assistant_text", "success": True, "data": {"text": "{\\\"action\\\":\\\"request_review\\\",\\\"reason\\\":\\\"review\\\",\\\"replacement_steps\\\":[],\\\"expected_postconditions\\\":[],\\\"confidence\\\":0.4}"}}), flush=True)
""".strip(),
        "utf-8",
    )
    extension = tmp_path / "provider.mjs"
    extension.write_text("test", "utf-8")
    client = PiRpcClient(
        PiRuntime(Path(sys.executable), script, extension, "fake"),
        api_base="https://example.com/v1",
        model="test-model",
        api_key="secret",
        state_root=tmp_path / "state",
        session_name="test",
        timeout_seconds=5,
    )
    try:
        response = client.prompt("synthetic failure")
        assert json.loads(response)["action"] == "request_review"
        assert client.running
    finally:
        client.close()
    assert not client.running


def test_real_pi_runtime_calls_local_compatible_endpoint_without_tools(tmp_path):
    requests = []
    completion = _replacement()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            chunks = [
                {
                    "id": "chatcmpl-pi-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": completion},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-pi-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                },
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(
                    b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, _format, *_args):
            return

    try:
        runtime = discover_pi_runtime()
    except Exception as exc:
        pytest.skip(f"optional Pi runtime is unavailable: {exc}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = PiSupervisorService(
        ModelSettings(
            f"http://127.0.0.1:{server.server_port}/v1",
            "test-model",
            "local-test-key",
            timeout_seconds=15,
        ),
        build_tool_registry(),
        tmp_path / "real-pi",
        session_name="local-integration-test",
        runtime=runtime,
    )
    try:
        outcome = service.supervise(_context())
        assert outcome.plan is not None
        assert requests
        assert not requests[0].get("tools")
    finally:
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
