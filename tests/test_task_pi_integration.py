"""Real isolated Pi process, synthetic local model replies, and an in-memory CAD worker."""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sw_assistant.agent.pi_client import PiUnavailableError, discover_pi_runtime
from sw_assistant.agent.task_backend import TaskAgentBackend
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import ModelSnapshot, StepResult
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry


class MemoryCad:
    """Small fixture with local postcondition evidence; no COM or file authority."""

    def __init__(self):
        self.snapshot = ModelSnapshot()
        self.calls = []

    def call(self, call, timeout_seconds=120):
        self.calls.append(call.tool)
        before = copy.deepcopy(self.snapshot)
        data = {}
        if call.tool == "model.observe":
            data = {"snapshot": self.snapshot.to_dict(), "complete": True,
                    "features": [{"name": f"Feature{index}", "type": "Boss", "error_code": 0}
                                 for index in range(self.snapshot.feature_count)],
                    "planes": [{"name": "Front Plane"}], "dimensions": []}
        elif call.tool == "session.ensure":
            self.snapshot.connected = True
        elif call.tool == "document.new_part":
            self.snapshot = ModelSnapshot(connected=True, document_title="SyntheticCube",
                                          document_type="part", feature_count=3)
        elif call.tool == "sketch.begin":
            self.snapshot.active_sketch = "Sketch1"
        elif call.tool == "sketch.rectangle":
            self.snapshot.sketch_entity_count = 4
            data = {"applied": {"tool": call.tool,
                                "frame": {"origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0]},
                                "center_m": [0, 0, 0], "corner_m": [0.005, 0.005, 0]}}
        elif call.tool == "feature.extrude":
            self.snapshot.active_sketch = ""
            self.snapshot.sketch_entity_count = 0
            self.snapshot.feature_count += 1
            self.snapshot.body_count = 1
            self.snapshot.volume_m3 = 0.000001
            data = {"applied": {"tool": call.tool, "depth_m": 0.01,
                                "operation": "boss", "end_condition": "blind",
                                "reverse": False, "merge": True}}
        else:
            raise AssertionError(f"unexpected fixture operation: {call.tool}")
        return StepResult(True, call, "synthetic operation verified", data=data,
                          before=before, after=copy.deepcopy(self.snapshot))

    def interrupt(self):
        pass

    def close(self):
        pass


def test_real_pi_continuous_task_reads_success_feedback_and_completes(tmp_path):
    try:
        discover_pi_runtime()
    except PiUnavailableError as exc:
        pytest.skip(f"optional installed Pi runtime unavailable: {exc}")
    decisions = [
        {"action": "act", "reason": "prepare the document", "phase": "prepare",
         "goals": [{"id": "volume", "description": "10 mm cube volume is 1000 mm3",
                    "metric": "volume_mm3", "expected": 1000.0, "tolerance": 0.001}],
         "requirements": [{"id": "r1", "goal_ids": ["volume"]}],
         "steps": [{"tool": "session.ensure"}, {"tool": "document.new_part"}]},
        {"action": "act", "reason": "document preparation succeeded; build the cube",
         "phase": "body", "steps": [
             {"tool": "sketch.begin", "arguments": {"plane": "front"}},
             {"tool": "sketch.rectangle", "arguments": {"width_mm": 10, "height_mm": 10}},
             {"tool": "feature.extrude", "arguments": {"depth_mm": 10}}]},
        {"action": "complete", "reason": "observed body has target volume; request independent verification"},
    ]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            decision = decisions[min(len(requests) - 1, len(decisions) - 1)]
            chunks = [
                {"id": f"chatcmpl-task-{len(requests)}", "object": "chat.completion.chunk",
                 "created": 1, "model": "local-task-model", "choices": [
                     {"index": 0, "delta": {"role": "assistant", "content": json.dumps(decision)},
                      "finish_reason": None}]},
                {"id": f"chatcmpl-task-{len(requests)}", "object": "chat.completion.chunk",
                 "created": 1, "model": "local-task-model", "choices": [
                     {"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry = build_tool_registry()
    backend = TaskAgentBackend(
        ModelSettings(f"http://127.0.0.1:{server.server_port}/v1", "local-task-model",
                      "local-only-fixture-key", timeout_seconds=20),
        registry, tmp_path / "agent", session_id="real-pi-v5-continuous", backend="pi",
    )
    worker = MemoryCad()
    runner = ContinuousTaskRunner(registry, worker, backend, TaskStore(tmp_path / "tasks"), max_rounds=5)
    backend.set_investigator(runner.investigate)
    try:
        assert runner.prepare("Create a 10 mm cube")["status"] == "ready"
        assert worker.calls == []
        client = backend._pi
        result = runner.run()
        assert result["status"] == "completed", result.get("message")
        assert result["verification"]["passed"]
        assert backend._pi is client
        assert len(requests) == 3
        assert all({tool["function"]["name"] for tool in request.get("tools", [])}
                   == {"sw_investigate"} for request in requests)
        assert sum(message["role"] == "assistant" for message in requests[2]["messages"]) >= 2
        last_payload = json.dumps(requests[2]["messages"], ensure_ascii=False)
        assert "completed_evidence" in last_payload
        assert "stage_succeeded" in last_payload
        assert "1000.0" in last_payload and "goals" in last_payload
        assert "prepare the document" in last_payload
        assert [call for call in worker.calls if call != "model.observe"] == [
            "session.ensure", "document.new_part", "sketch.begin", "sketch.rectangle", "feature.extrude",
        ]
    finally:
        backend.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
