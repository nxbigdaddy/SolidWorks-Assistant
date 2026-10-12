"""Document binding and production worker telemetry, with synthetic COM only."""
from __future__ import annotations

import contextlib
import queue
from types import SimpleNamespace

import pytest

from sw_assistant.domain import ModelSnapshot, ToolCall
from sw_assistant.solidworks.gateway import SolidWorksGateway
from sw_assistant.solidworks.preconditions import StateChanged
from sw_assistant.solidworks import worker as worker_module


def gateway_fixture(monkeypatch):
    gateway = SolidWorksGateway()
    document = SimpleNamespace(identity="one", GetType=lambda: 1)
    gateway._sw = SimpleNamespace(ActiveDoc=document)
    monkeypatch.setattr(gateway, "_com_identity", lambda obj: obj.identity)
    return gateway, document


def test_bound_object_is_retained_even_when_com_returns_another_wrapper(monkeypatch):
    gateway, document = gateway_fixture(monkeypatch)
    with gateway.execution_document({"document_identity": "one"}):
        gateway._sw.ActiveDoc = SimpleNamespace(identity="one", GetType=lambda: 1)
        assert gateway.require_document(part=True) is document
    assert gateway.require_document() is gateway._sw.ActiveDoc


def test_active_doc_switch_cannot_redirect_a_bound_operation(monkeypatch):
    gateway, _ = gateway_fixture(monkeypatch)
    with gateway.execution_document({"document_identity": "one"}):
        gateway._sw.ActiveDoc = SimpleNamespace(identity="two", GetType=lambda: 1)
        with pytest.raises(StateChanged, match="绑定目标"):
            gateway.require_document()
    assert gateway._execution_document is None


def test_scope_refuses_changed_identity_and_nested_binding(monkeypatch):
    gateway, _ = gateway_fixture(monkeypatch)
    with pytest.raises(StateChanged):
        with gateway.execution_document({"document_identity": "other"}):
            pytest.fail("must refuse before execute")
    with gateway.execution_document({"document_identity": "one"}):
        with pytest.raises(StateChanged, match="嵌套"):
            with gateway.execution_document({"document_identity": "one"}):
                pytest.fail("nested execution is forbidden")
    with pytest.raises(RuntimeError):
        with gateway.execution_document({"document_identity": "one"}):
            raise RuntimeError("synthetic native error")
    assert gateway._execution_document is None


@pytest.mark.parametrize("complete", [True, False])
def test_strict_observe_avoids_two_extra_best_effort_traversals(monkeypatch, complete):
    events = []
    snapshot = ModelSnapshot(document_type="part", document_title="synthetic").to_dict()

    class Gateway:
        def __init__(self, visible):
            pass

        def snapshot(self):
            pytest.fail("strict observe must not trigger a second geometry path")

        def execute(self, call):
            events.append(call.tool)
            return "observed", {"complete": complete, "snapshot": snapshot}

        def release(self, close_owned):
            pass

    monkeypatch.setattr(worker_module, "SolidWorksGateway", Gateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", lambda _: contextlib.nullcontext())
    requests, responses = queue.Queue(), queue.Queue()
    requests.put({"kind": "call", "id": "observe", "progress": True,
                  "call": ToolCall("model.observe", {}).to_dict()})
    requests.put({"kind": "shutdown"})
    worker_module.worker_main(requests, responses)
    output = []
    while not responses.empty():
        output.append(responses.get_nowait())
    assert events == ["model.observe"]
    assert [item["phase"] for item in output[:-1]] == [
        "waiting_gate", "before_observation", "executing", "after_observation"]
    assert all(set(item) == {"id", "kind", "phase"} for item in output[:-1])
    assert output[-1]["result"]["after"] == (snapshot if complete else None)


@pytest.mark.parametrize("operation", ["list", "restore"])
def test_host_checkpoint_rpc_is_strict_and_not_a_model_tool(monkeypatch, operation):
    calls = []

    class Gateway:
        def __init__(self, visible):
            pass

        def configure_candidate_workspace(self, path):
            pass

        def checkpoint_request(self, operation, task_id, checkpoint_id):
            calls.append((operation, task_id, checkpoint_id))
            return {"ok": True, "status": "synthetic"}

        def execute(self, call):
            pytest.fail("checkpoint recovery must not pass through ToolCall")

        def release(self, close_owned):
            pass

    monkeypatch.setattr(worker_module, "SolidWorksGateway", Gateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", lambda _: contextlib.nullcontext())
    request = {"kind": "checkpoint", "id": "checkpoint", "operation": operation, "task_id": "t"}
    if operation == "restore":
        request["checkpoint_id"] = "c"
    requests, responses = queue.Queue(), queue.Queue()
    requests.put({**request, "unsafe_path": "arbitrary"})
    requests.put(request)
    requests.put({"kind": "shutdown"})
    worker_module.worker_main(requests, responses, "synthetic-workspace")
    assert responses.get_nowait()["ok"] is False
    assert responses.get_nowait()["ok"] is True
    assert calls == [(operation, "t", "c" if operation == "restore" else None)]
