import contextlib
import queue

import sw_assistant.solidworks.worker as worker_module
from sw_assistant.domain import ModelSnapshot
from sw_assistant.solidworks.worker import worker_main
import pytest


def test_worker_returns_structured_failure_for_malformed_request_and_continues():
    requests = queue.Queue()
    responses = queue.Queue()
    requests.put(
        {
            "kind": "call",
            "id": "bad-request",
            "call": {"tool": "", "arguments": []},
        }
    )
    requests.put({"kind": "shutdown"})
    worker_main(requests, responses)
    response = responses.get_nowait()
    assert response["id"] == "bad-request"
    assert response["result"]["ok"] is False
    assert response["result"]["call"]["tool"] == "invalid.request"


def test_failure_snapshot_is_captured_before_serial_gate_releases(monkeypatch):
    events = []
    gate_depth = 0

    @contextlib.contextmanager
    def fake_gate(_timeout):
        nonlocal gate_depth
        gate_depth += 1
        events.append("gate-enter")
        try:
            yield
        finally:
            events.append("gate-exit")
            gate_depth -= 1

    class FakeGateway:
        def __init__(self, visible):
            assert visible is True
            self.snapshot_count = 0

        def snapshot(self):
            assert gate_depth == 1
            self.snapshot_count += 1
            events.append(f"snapshot-{self.snapshot_count}")
            return ModelSnapshot(document_type="part", document_title="Part1")

        @staticmethod
        def execute(_call):
            events.append("execute")
            raise RuntimeError("simulated COM failure")

        @staticmethod
        def release(*, close_owned):
            assert close_owned is True

    monkeypatch.setattr(worker_module, "SolidWorksGateway", FakeGateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", fake_gate)
    requests = queue.Queue()
    responses = queue.Queue()
    requests.put(
        {
            "kind": "call",
            "id": "failure",
            "call": {"tool": "model.inspect", "arguments": {}},
        }
    )
    requests.put({"kind": "shutdown"})

    worker_module.worker_main(requests, responses)
    result = responses.get_nowait()["result"]

    assert result["ok"] is False
    assert result["before"]["document_title"] == "Part1"
    assert result["after"]["document_title"] == "Part1"
    assert events == ["gate-enter", "snapshot-1", "execute", "snapshot-2", "gate-exit"]


@pytest.mark.parametrize("tool,executed", [("session.capabilities", True), ("sketch.inspect", True),
                                         ("feature.extrude", False), ("session.ensure", False)])
def test_focused_reads_survive_snapshot_failure_without_relaxing_writes(monkeypatch, tool, executed):
    calls = []

    class Gateway:
        def __init__(self, visible):
            pass

        def snapshot(self):
            raise RuntimeError("broad snapshot getter failed")

        def execute(self, call):
            calls.append(call.tool)
            return "focused read", {"complete": False, "issues": ["snapshot unavailable"]}

        def release(self, close_owned):
            pass

    monkeypatch.setattr(worker_module, "SolidWorksGateway", Gateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", lambda _: contextlib.nullcontext())
    requests, responses = queue.Queue(), queue.Queue()
    requests.put({"id": "read", "call": {"tool": tool, "arguments": {}}})
    requests.put({"kind": "shutdown"})
    worker_main(requests, responses)
    result = responses.get_nowait()["result"]
    assert bool(calls) is executed and result["ok"] is executed
    assert result["before"] is None
