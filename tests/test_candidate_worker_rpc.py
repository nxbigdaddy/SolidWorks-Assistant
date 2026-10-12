"""Private candidate RPC boundaries, with fake queues and no SolidWorks access."""
from __future__ import annotations

import contextlib
import queue
import threading

import pytest

import sw_assistant.solidworks.worker as worker_module
from sw_assistant.domain import ToolError
from sw_assistant.domain.errors import WorkerError
from sw_assistant.solidworks.client import SolidWorksWorkerClient


EXPECTED = {"complete": True, "document_identity": "synthetic:baseline", "snapshot": {}}
OPENED = {"ok": True, "token": "host-token", "candidate": {"complete": True}}


class Process:
    def __init__(self):
        self.running = True

    def is_alive(self):
        return self.running

    def terminate(self):
        self.running = False

    def join(self, timeout=None):
        pass


def client(monkeypatch, tmp_path, *, enabled=True, response_changes=None, on_get=None):
    value = SolidWorksWorkerClient(tmp_path / "host-candidates" if enabled else None)
    requests, sent, starts = queue.Queue(), [], []

    class Responses:
        def get(self, timeout=None):
            request = requests.get_nowait()
            sent.append(request)
            if on_get:
                on_get(value)
            response = {"kind": "candidate", "id": request["id"], "ok": True, "data": dict(OPENED)}
            response.update(response_changes or {})
            return response

    value._process = Process()
    value._requests, value._responses = requests, Responses()
    monkeypatch.setattr(value, "start", lambda: starts.append(True))
    return value, sent, starts


@pytest.mark.parametrize(("operation", "payload"), [
    ("begin", {"expected": EXPECTED, "task_id": "synthetic-task"}),
    ("finish", {"expected": EXPECTED, "token": "host-token", "accept": True}),
    ("finish", {"expected": EXPECTED, "token": "host-token", "accept": False}),
])
def test_candidate_client_roundtrip_is_private_and_has_no_workspace_payload(
    monkeypatch, tmp_path, operation, payload,
):
    value, sent, _ = client(monkeypatch, tmp_path)
    assert value.candidate_request(operation, **payload) == OPENED
    assert len(sent) == 1
    assert set(sent[0]) == {"kind", "id", "operation", *payload}
    assert sent[0]["kind"] == "candidate" and sent[0]["operation"] == operation
    assert "call" not in sent[0]


@pytest.mark.parametrize("operation", ["undo", "save", "close", "shell", "", None])
def test_candidate_client_operation_allowlist_rejects_before_start(monkeypatch, tmp_path, operation):
    value, sent, starts = client(monkeypatch, tmp_path)
    with pytest.raises(WorkerError):
        value.candidate_request(operation, expected=EXPECTED, task_id="task")
    assert not sent and not starts


@pytest.mark.parametrize("field", ["workspace", "candidate_workspace", "path", "root", "timeout_seconds"])
def test_candidate_client_cannot_inject_host_workspace_or_rpc_fields(monkeypatch, tmp_path, field):
    value, sent, starts = client(monkeypatch, tmp_path)
    with pytest.raises(WorkerError):
        value.candidate_request("begin", expected=EXPECTED, task_id="task", **{field: "untrusted"})
    assert not sent and not starts


@pytest.mark.parametrize("accept", [None, 0, 1, "true", "false", [], {}])
def test_candidate_client_rejects_non_boolean_accept_before_start(monkeypatch, tmp_path, accept):
    value, sent, starts = client(monkeypatch, tmp_path)
    with pytest.raises(WorkerError):
        value.candidate_request("finish", expected=EXPECTED, token="host-token", accept=accept)
    assert not sent and not starts


def test_candidate_client_requires_configured_host_workspace(monkeypatch, tmp_path):
    value, sent, starts = client(monkeypatch, tmp_path, enabled=False)
    assert value.candidate_workspace_enabled is False
    with pytest.raises(WorkerError):
        value.candidate_request("begin", expected=EXPECTED, task_id="task")
    assert not sent and not starts


@pytest.mark.parametrize(("operation", "payload"), [
    ("begin", {"expected": EXPECTED}),
    ("finish", {"expected": EXPECTED, "accept": True}),
    ("finish", {"expected": EXPECTED, "token": "host-token"}),
])
def test_candidate_client_requires_exact_payload_fields(monkeypatch, tmp_path, operation, payload):
    value, sent, starts = client(monkeypatch, tmp_path)
    with pytest.raises(WorkerError):
        value.candidate_request(operation, **payload)
    assert not sent and not starts


def test_candidate_workspace_is_passed_only_at_host_process_creation(tmp_path):
    workspace = tmp_path / "host-candidates"
    value = SolidWorksWorkerClient(workspace)
    process = Process()
    process.start = lambda: None
    spawned = []

    class Context:
        Queue = staticmethod(queue.Queue)

        def Process(self, **kwargs):
            spawned.append(kwargs)
            return process

    value._context = Context()
    value.start()
    assert len(spawned) == 1
    assert spawned[0]["target"] is worker_module.worker_main
    assert spawned[0]["args"] == (value._requests, value._responses, str(workspace.resolve()), True)


@pytest.mark.parametrize("changes", [
    {"ok": False, "error": "candidate refused"}, {"kind": "call"}, {"ok": 1},
    {"ok": "true"}, {"data": None}, {"data": []},
])
def test_candidate_client_rejects_invalid_response(monkeypatch, tmp_path, changes):
    value, _, _ = client(monkeypatch, tmp_path, response_changes=changes)
    with pytest.raises(WorkerError):
        value.candidate_request("begin", expected=EXPECTED, task_id="task")


def test_candidate_stopped_operation_cannot_restart_helper(monkeypatch, tmp_path):
    value, sent, starts = client(monkeypatch, tmp_path)
    with value.operation():
        value.interrupt()
        with pytest.raises(WorkerError, match="已被停止"):
            value.candidate_request("begin", expected=EXPECTED, task_id="task")
    assert not sent and not starts


@pytest.mark.parametrize("late_success", [False, True])
@pytest.mark.parametrize(("operation", "payload"), [
    ("begin", {"expected": EXPECTED, "task_id": "task"}),
    ("finish", {"expected": EXPECTED, "token": "host-token", "accept": True}),
    ("finish", {"expected": EXPECTED, "token": "host-token", "accept": False}),
])
def test_stop_while_candidate_waits_discards_late_response_without_restart(
    monkeypatch, tmp_path, late_success, operation, payload,
):
    def stop(value):
        value.interrupt()
        if not late_success:
            raise queue.Empty

    value, sent, starts = client(monkeypatch, tmp_path, on_get=stop)
    with pytest.raises(WorkerError, match="已停止"):
        value.candidate_request(operation, **payload)
    assert len(sent) == 1 and len(starts) == 1
    assert not value.alive


def test_candidate_waiting_for_call_lock_cannot_restart_after_stop(monkeypatch, tmp_path):
    value, sent, starts = client(monkeypatch, tmp_path)
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
            value.candidate_request("begin", expected=EXPECTED, task_id="task")
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


def worker_roundtrip(monkeypatch, tmp_path, request, *, configured=True, failure=False):
    events = []
    depth = 0
    workspace = str((tmp_path / "host-owned-workspace").resolve())

    @contextlib.contextmanager
    def gate(timeout):
        nonlocal depth
        assert timeout == 120
        depth += 1
        events.append("enter")
        try:
            yield
        finally:
            depth -= 1
            events.append("exit")

    class Gateway:
        def __init__(self, visible):
            assert visible is True

        def configure_candidate_workspace(self, actual):
            assert actual == workspace
            events.append("configure")

        def begin_candidate(self, expected, task_id):
            assert depth == 1 and configured
            assert expected == EXPECTED and task_id == "synthetic-task"
            events.append("begin")
            if failure:
                raise ToolError("synthetic candidate failure")
            return dict(OPENED)

        def finish_candidate(self, token, accept, expected):
            assert depth == 1 and configured
            assert token == "host-token" and type(accept) is bool and expected == EXPECTED
            events.append("accept" if accept else "reject")
            if failure:
                raise ToolError("synthetic candidate failure")
            return {"ok": True, "status": "committed" if accept else "restored"}

        def __getattr__(self, name):
            raise AssertionError(f"Candidate RPC must not access unrelated gateway method {name}")

        def release(self, *, close_owned):
            assert close_owned is True
            events.append("release")

    monkeypatch.setattr(worker_module, "SolidWorksGateway", Gateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", gate)
    requests, responses = queue.Queue(), queue.Queue()
    requests.put({"kind": "candidate", "id": "test-candidate", **request})
    requests.put({"kind": "shutdown"})
    worker_module.worker_main(requests, responses, workspace if configured else None)
    response = responses.get_nowait()
    assert response["kind"] == "candidate" and response["id"] == "test-candidate"
    assert responses.empty()
    return response, events


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize(("candidate_request", "method"), [
    ({"operation": "begin", "expected": EXPECTED, "task_id": "synthetic-task"}, "begin"),
    ({"operation": "finish", "expected": EXPECTED, "token": "host-token", "accept": True}, "accept"),
    ({"operation": "finish", "expected": EXPECTED, "token": "host-token", "accept": False}, "reject"),
])
def test_candidate_worker_uses_shared_serial_gate_for_begin_and_finish(
    monkeypatch, tmp_path, candidate_request, method, failure,
):
    response, events = worker_roundtrip(monkeypatch, tmp_path, candidate_request, failure=failure)
    assert response["ok"] is (not failure)
    if failure:
        assert response["error_code"] == "ToolError" and "data" not in response
    else:
        assert response["data"]["ok"] is True
    assert events == ["configure", "enter", method, "exit", "release"]


@pytest.mark.parametrize("operation", ["undo", "save", "close", "shell", "", None])
def test_candidate_worker_operation_allowlist_is_independent(monkeypatch, tmp_path, operation):
    response, events = worker_roundtrip(monkeypatch, tmp_path, {
        "operation": operation, "expected": EXPECTED, "task_id": "synthetic-task",
    })
    assert response["ok"] is False and response["error_code"] == "ValueError"
    assert events == ["configure", "release"]


@pytest.mark.parametrize("field", ["workspace", "candidate_workspace", "path", "root", "timeout_seconds"])
def test_candidate_worker_rejects_injected_workspace_before_gateway_call(monkeypatch, tmp_path, field):
    response, events = worker_roundtrip(monkeypatch, tmp_path, {
        "operation": "begin", "expected": EXPECTED, "task_id": "synthetic-task", field: "untrusted",
    })
    assert response["ok"] is False and response["error_code"] == "ValueError"
    assert events == ["configure", "release"]


@pytest.mark.parametrize("accept", [None, 0, 1, "true", "false", [], {}])
def test_candidate_worker_independently_rejects_non_boolean_accept(monkeypatch, tmp_path, accept):
    response, events = worker_roundtrip(monkeypatch, tmp_path, {
        "operation": "finish", "expected": EXPECTED, "token": "host-token", "accept": accept,
    })
    assert response["ok"] is False and "accept" in response["error"]
    assert "accept" not in events and "reject" not in events
    assert events[-1] == "release"


def test_unconfigured_worker_cannot_enable_candidates_from_payload(monkeypatch, tmp_path):
    response, events = worker_roundtrip(monkeypatch, tmp_path, {
        "operation": "begin", "expected": EXPECTED, "task_id": "synthetic-task",
    }, configured=False)
    assert response["ok"] is False
    assert events == ["release"]


@pytest.mark.parametrize("payload", [
    {"operation": "begin", "expected": EXPECTED},
    {"operation": "finish", "expected": EXPECTED, "accept": True},
    {"operation": "finish", "expected": EXPECTED, "token": "host-token"},
])
def test_candidate_worker_independently_requires_exact_payload_fields(monkeypatch, tmp_path, payload):
    response, events = worker_roundtrip(monkeypatch, tmp_path, payload)
    assert response["ok"] is False
    assert events == ["configure", "release"]
