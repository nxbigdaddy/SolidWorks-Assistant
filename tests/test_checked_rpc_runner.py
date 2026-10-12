"""Continuous runner through the production RPC loop; no CAD or model access."""
from __future__ import annotations

import copy
import queue
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import sw_assistant.solidworks.worker as worker_module
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import ModelSnapshot, ToolCall
from sw_assistant.solidworks.client import SolidWorksWorkerClient
from sw_assistant.tools import build_tool_registry

from test_continuous_task_runner import Backend, Worker, act, call, complete


class NativeDocument:
    """Only constant native headers are simulated, never an actual COM object."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.ConfigurationManager = SimpleNamespace(
            ActiveConfiguration=SimpleNamespace(Name="Default", identity="config:candidate"),
        )
        self.identity = "document:candidate"

    def GetTitle(self):
        return self.gateway.model.snapshot.document_title

    def GetPathName(self):
        return self.gateway.model.snapshot.document_path

    def GetType(self):
        return 1

    def GetUpdateStamp(self):
        return self.gateway.stamp


class FakeGateway:
    def __init__(self):
        self.model = Worker()
        self.stamp = 0
        self.document = NativeDocument(self)
        self.inject_source_snapshot = False
        self.snapshot_injections = 0
        self.executed = []
        self.identity_reads = []
        self.released = False

    @staticmethod
    def _com_identity(document):
        return document.identity

    def snapshot(self):
        if self.inject_source_snapshot:
            self.inject_source_snapshot = False
            self.snapshot_injections += 1
            return ModelSnapshot(connected=True, document_title="Preserved Source",
                                 document_type="part", feature_count=18)
        return copy.deepcopy(self.model.snapshot)

    def probe_document_identity(self):
        state = self.model.snapshot
        identity = self.document.identity if state.document_type == "part" else None
        self.identity_reads.append(identity)
        return {
            "connected": state.connected,
            "document_identity": identity,
            "document_title": state.document_title,
            "document_path": state.document_path,
            "document_type": state.document_type,
        }

    def require_document(self, *, part=False):
        assert self.model.snapshot.document_type == "part"
        return self.document

    def _observe_model(self, include_configuration=False):
        result = self.model.call(ToolCall("model.observe", {})).data
        has_document = self.model.snapshot.document_type == "part"
        result.update(
            document_identity=self.document.identity if has_document else None,
            configuration="Default" if has_document else None,
            configuration_identity="config:candidate" if has_document else None,
            model_update_stamp=self.stamp if has_document else None,
        )
        return result

    def execute(self, tool_call):
        self.executed.append(tool_call.tool)
        if tool_call.tool == "model.observe":
            return "Observed synthetic document", self._observe_model()
        if tool_call.tool == "sketch.rectangle":
            arguments = tool_call.arguments
            center_x = arguments["center_x_mm"] / 1000
            center_y = arguments["center_y_mm"] / 1000
            self.model.snapshot.sketch_entity_count += 6
            self.stamp += 1
            return "Created synthetic rectangle", {"applied": {
                "tool": tool_call.tool,
                "frame": {"origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0]},
                "center_m": [center_x, center_y, 0],
                "corner_m": [center_x + arguments["width_mm"] / 2000,
                             center_y + arguments["height_mm"] / 2000, 0],
            }}
        result = self.model.call(tool_call)
        assert result.ok
        if tool_call.tool == "document.new_part":
            self.model.snapshot.document_title = "Candidate"
            self.model.snapshot.document_path = "candidate.SLDPRT"
        self.stamp += 1
        return result.message, result.data

    def release(self, **kwargs):
        self.released = True


class InMemoryProcess:
    """Production worker_main, substituting only OS process transport."""

    def __init__(self, *, target, args, **kwargs):
        self.thread = threading.Thread(target=target, args=args, daemon=True)

    def start(self):
        self.thread.start()

    def is_alive(self):
        return self.thread.is_alive()

    def join(self, timeout=None):
        self.thread.join(timeout)

    def terminate(self):
        raise AssertionError("Synthetic RPC must finish by graceful worker shutdown")


@pytest.fixture
def rpc_runner(monkeypatch, tmp_path):
    active_clients = []

    def build(*, inject_source=False, sketch_tool="sketch.circle"):
        gateway = FakeGateway()
        captured_requests = []

        class Requests(queue.Queue):
            def put(self, value, *args, **kwargs):
                captured_requests.append(copy.deepcopy(value))
                if (inject_source and value.get("kind") == "checked_call"
                        and value.get("call", {}).get("tool") == sketch_tool):
                    gateway.inject_source_snapshot = True
                return super().put(value, *args, **kwargs)

        class Context:
            def __init__(self):
                self.queue_count = 0

            def Queue(self):
                self.queue_count += 1
                return Requests() if self.queue_count == 1 else queue.Queue()

            Process = staticmethod(InMemoryProcess)

        monkeypatch.setattr(worker_module, "SolidWorksGateway", lambda **kwargs: gateway)
        monkeypatch.setattr(worker_module, "solidworks_serial_gate", lambda timeout: nullcontext())
        client = SolidWorksWorkerClient()
        client._context = Context()
        active_clients.append(client)
        sketch_call = (call("sketch.circle", radius_mm=5) if sketch_tool == "sketch.circle"
                       else call("sketch.rectangle", width_mm=10, height_mm=10))
        backend = Backend([
            act([call("session.ensure"), call("document.new_part")]),
            act([call("sketch.begin", plane="front"), sketch_call,
                 call("feature.extrude", depth_mm=10)], goals=[]),
            complete(),
        ])
        runner = ContinuousTaskRunner(build_tool_registry(), client, backend,
                                      TaskStore(tmp_path / "tasks"))
        return runner, client, gateway, captured_requests

    yield build
    for client in active_clients:
        client.close()


@pytest.mark.parametrize("sketch_tool", ["sketch.circle", "sketch.rectangle"])
def test_stable_stage_uses_checked_rpc_and_completes(rpc_runner, sketch_tool):
    runner, _, gateway, requests = rpc_runner(sketch_tool=sketch_tool)
    assert runner.prepare("Create a synthetic part with volume 1000 mm3")["status"] == "ready"
    result = runner.run()
    assert result["status"] == "completed"
    assert result["verification"]["passed"] is True
    checked = [item for item in requests if item.get("kind") == "checked_call"]
    assert [item["call"]["tool"] for item in checked] == [
        "sketch.begin", sketch_tool, "feature.extrude",
    ]
    assert all(item["expected"]["complete"] is True for item in checked)
    assert all(item["expected"]["document_identity"] == "document:candidate" for item in checked)
    assert gateway.model.snapshot.body_count == 1
    assert len(result["completed_steps"]) == 5


@pytest.mark.parametrize("sketch_tool", ["sketch.circle", "sketch.rectangle"])
def test_transient_source_snapshot_is_rejected_before_cad_execution(rpc_runner, sketch_tool):
    runner, _, gateway, requests = rpc_runner(inject_source=True, sketch_tool=sketch_tool)
    assert runner.prepare("Create a synthetic part with volume 1000 mm3")["status"] == "ready"
    result = runner.run()
    assert result["status"] == "review"
    assert gateway.snapshot_injections == 1
    assert sketch_tool not in gateway.executed
    assert "feature.extrude" not in gateway.executed
    assert gateway.model.snapshot.sketch_entity_count == 0
    assert gateway.model.snapshot.body_count == 0
    assert "尚未执行" in result["message"]
    assert "不要" in result["message"] and "撤销" in result["message"]
    failure = result["failure_attempts"][-1]["result"]
    assert failure["data"]["execution"]["started"] is False
    assert failure["before"]["document_title"] == "Preserved Source"
    assert result["latest_observation"]["document_identity"] == "document:candidate"
    assert len(result["completed_steps"]) == 3
    checked = [item for item in requests if item.get("kind") == "checked_call"]
    assert [item["call"]["tool"] for item in checked] == ["sketch.begin", sketch_tool]
    assert checked[-1]["expected"]["snapshot"]["active_sketch"] == "Sketch1"
    assert checked[-1]["expected"]["snapshot"]["sketch_entity_count"] == 0
