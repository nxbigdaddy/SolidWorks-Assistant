"""Private host RPC tests use in-memory queues and synthetic worker processes."""
from __future__ import annotations

import contextlib
import queue
import threading

import pytest

import sw_assistant.solidworks.worker as worker_module
from sw_assistant.domain import ToolError
from sw_assistant.domain.errors import WorkerError
from sw_assistant.solidworks.client import SolidWorksWorkerClient


EMPTY = {"connected": False, "document_identity": None, "document_title": "",
         "document_path": "", "document_type": "none"}
PART = {"connected": True, "document_identity": "synthetic:document", "document_title": "Part1",
        "document_path": "", "document_type": "part"}


class Process:
    def __init__(self):
        self.running = True

    def is_alive(self):
        return self.running

    def terminate(self):
        self.running = False

    def join(self, timeout=None):
        pass


def client(monkeypatch, identity=None, response_changes=None, on_get=None):
    value = SolidWorksWorkerClient()
    requests = queue.Queue()
    sent = []
    starts = []

    class Responses:
        def get(self, timeout=None):
            request = requests.get_nowait()
            sent.append(request)
            if on_get:
                on_get(value)
            result = {"kind": "identity", "id": request["id"], "ok": True,
                      "identity": dict(EMPTY if identity is None else identity)}
            result.update(response_changes or {})
            return result

    value._process = Process()
    value._requests, value._responses = requests, Responses()
    monkeypatch.setattr(value, "start", lambda: starts.append(True))
    return value, sent, starts


@pytest.mark.parametrize("identity", [EMPTY, {**EMPTY, "connected": True}, PART])
def test_private_probe_roundtrip_does_not_submit_tool_calls(monkeypatch, identity):
    value, sent, _ = client(monkeypatch, identity)
    assert value.probe_document_identity() == identity
    assert len(sent) == 1
    assert sent[0]["kind"] == "identity"
    assert sent[0]["timeout_seconds"] == 30
    assert "call" not in sent[0]


@pytest.mark.parametrize("identity", [
    {}, {**PART, "document_identity": None}, {**PART, "document_identity": ""},
    {**PART, "connected": False}, {**PART, "document_path": None},
    {**PART, "document_type": "unknown"}, {**EMPTY, "document_title": "Part1"},
    {**EMPTY, "document_identity": "unknown-document"}, {**PART, "connected": 1},
])
def test_private_probe_unknown_identity_fails_closed(monkeypatch, identity):
    value, _, _ = client(monkeypatch, identity)
    with pytest.raises(WorkerError):
        value.probe_document_identity()


@pytest.mark.parametrize("changes", [
    {"ok": False, "error": "unknown identity"}, {"kind": "call"}, {"ok": "true"},
])
def test_private_probe_rejects_error_or_wrong_rpc_response(monkeypatch, changes):
    value, _, _ = client(monkeypatch, PART, changes)
    with pytest.raises(WorkerError):
        value.probe_document_identity()


def test_private_probe_returns_only_header_fields(monkeypatch):
    value, _, _ = client(monkeypatch, {**PART, "features": [{"name": "not requested"}]})
    assert value.probe_document_identity() == PART


def test_stopped_operation_cannot_restart_helper_for_identity_probe(monkeypatch):
    value, sent, starts = client(monkeypatch)
    with value.operation():
        value.interrupt()
        with pytest.raises(WorkerError, match="已被停止"):
            value.probe_document_identity()
    assert not starts and not sent


def test_stop_during_identity_response_discards_late_success(monkeypatch):
    value, sent, _ = client(monkeypatch, PART, on_get=lambda worker: worker.interrupt())
    with pytest.raises(WorkerError, match="已停止"):
        value.probe_document_identity()
    assert len(sent) == 1
    assert not value.alive


def test_queued_identity_probe_cannot_restart_after_stop(monkeypatch):
    value, sent, starts = client(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    class BlockedCallLock:
        def __enter__(self):
            entered.set()
            assert release.wait(2)

        def __exit__(self, *_args):
            pass

    value._call_lock = BlockedCallLock()
    errors = []

    def run():
        try:
            value.probe_document_identity()
        except WorkerError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(1)
    value.interrupt()
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors and "已被停止" in errors[0]
    assert not sent and not starts


@pytest.mark.parametrize("failure", [False, True])
def test_worker_private_probe_uses_gate_and_never_takes_snapshot_or_executes(monkeypatch, failure):
    events = []
    gate_depth = 0

    @contextlib.contextmanager
    def gate(timeout):
        nonlocal gate_depth
        assert timeout == 30
        gate_depth += 1
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
            gate_depth -= 1

    class Gateway:
        def __init__(self, visible):
            assert visible is True

        def probe_document_identity(self):
            assert gate_depth == 1
            events.append("identity")
            if failure:
                raise ToolError("unknown identity")
            return dict(PART)

        def __getattr__(self, name):
            raise AssertionError(f"Private probe must not access {name}")

        def release(self, *, close_owned):
            assert close_owned is True
            events.append("release")

    monkeypatch.setattr(worker_module, "SolidWorksGateway", Gateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", gate)
    requests, responses = queue.Queue(), queue.Queue()
    requests.put({"kind": "identity", "id": "probe"})
    requests.put({"kind": "shutdown"})
    worker_module.worker_main(requests, responses)
    response = responses.get_nowait()
    assert response["id"] == "probe" and response["kind"] == "identity"
    assert response["ok"] is (not failure)
    assert isinstance(response["duration_ms"], int) and response["duration_ms"] >= 0
    if failure:
        assert response["error_code"] == "ToolError" and "identity" not in response
    else:
        assert response["identity"] == PART
    assert events == ["enter", "identity", "exit", "release"]


def test_diagnostic_history_keeps_worker_duration_without_changing_novelty(tmp_path):
    from test_continuous_task_runner import act, runner

    task, worker, backend = runner(tmp_path, [act()])
    original = worker.call
    durations = iter([321, 654])

    def timed_call(call, timeout_seconds=120):
        result = original(call, timeout_seconds)
        result.duration_ms = next(durations)
        return result

    worker.call = timed_call
    backend.callback = lambda: (task.investigate("model.observe", {}), task.investigate("model.observe", {}))
    task.prepare("创建零件")
    history = task.snapshot()["diagnostic_history"]
    assert [item["duration_ms"] for item in history] == [321, 654]
    assert history[0]["fingerprint"] == history[1]["fingerprint"]
    assert task.store.load(task.snapshot()["id"])["diagnostic_history"][1]["duration_ms"] == 654
