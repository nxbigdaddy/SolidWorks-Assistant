"""Production client progress, fixed deadlines and private checkpoint RPC, offline."""
from __future__ import annotations

import copy
import queue
from types import SimpleNamespace

import pytest

import sw_assistant.solidworks.client as client_module
from sw_assistant.domain import StepResult, ToolCall
from sw_assistant.domain.errors import WorkerError
from sw_assistant.solidworks.client import SolidWorksWorkerClient


CHECKPOINT_ID = "a" * 32


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Process:
    def __init__(self):
        self.running = True
        self.terminations = 0

    def is_alive(self):
        return self.running

    def terminate(self):
        self.terminations += 1
        self.running = False

    def join(self, timeout=None):
        pass


def make_client(monkeypatch, tmp_path, *, hook=True, sequence=None, on_get=None):
    events, sent, starts = [], [], []
    clock, process = Clock(), Process()
    callback = (lambda name, data: events.append((name, copy.deepcopy(data)))) if hook is True else hook or None
    value = SolidWorksWorkerClient(tmp_path / "candidates", on_progress=callback)
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    pending = list(sequence or [])

    class Requests:
        def put(self, request):
            sent.append(copy.deepcopy(request))

    class Responses:
        def get(self, timeout=None):
            request = sent[-1]
            if on_get is not None:
                return on_get(value, request, clock, timeout)
            if pending:
                response = pending.pop(0)
                return {"id": request["id"], **response}
            if "call" in request:
                return {"id": request["id"], "result": StepResult(
                    True, ToolCall.from_mapping(request["call"]), "local fixture",
                ).to_dict()}
            if request["kind"] == "identity":
                return {"id": request["id"], "kind": "identity", "ok": True, "identity": {
                    "connected": False, "document_identity": None, "document_title": "",
                    "document_path": "", "document_type": "none",
                }}
            return {"id": request["id"], "kind": request["kind"], "ok": True, "data": {"ok": True}}

    value._requests, value._responses, value._process = Requests(), Responses(), process
    monkeypatch.setattr(value, "start", lambda: starts.append(True))
    return value, events, sent, starts, clock, process


def invoke(value, kind):
    if kind == "call":
        return value.call(ToolCall("model.observe", {}))
    if kind == "checked_call":
        return value.call_checked(ToolCall("sketch.circle", {"radius_mm": 2}), {"complete": True})
    if kind == "candidate":
        return value.candidate_request("begin", expected={"complete": True}, task_id="task")
    if kind == "checkpoint":
        return value.checkpoint_request("list", "task")
    return value.probe_document_identity()


@pytest.mark.parametrize("kind", ["call", "checked_call", "candidate", "checkpoint", "identity"])
@pytest.mark.parametrize("hook", [False, True])
def test_hook_opt_in_only_adds_private_progress_flag_and_consumes_phase_packets(monkeypatch, tmp_path, kind, hook):
    sequence = [{"kind": "progress", "phase": "waiting_gate"},
                {"kind": "progress", "phase": "executing", "parameters": {"secret": "private"}}]
    value, events, sent, starts, _, _ = make_client(monkeypatch, tmp_path, hook=hook, sequence=sequence)
    result = invoke(value, kind)
    assert result is not None and len(sent) == len(starts) == 1
    assert (sent[0].get("progress") is True) is hook
    if not hook:
        assert "progress" not in sent[0] and events == []
        return
    assert [name for name, _ in events] == ["rpc.started", "rpc.phase", "rpc.phase", "rpc.finished"]
    assert [data["phase"] for _, data in events] == ["waiting_client_lock", "waiting_gate", "executing", "completed"]
    assert all(set(data) <= {"request_id", "operation", "tool", "phase", "elapsed_seconds"} for _, data in events)
    assert all(data["request_id"] == sent[0]["id"] for _, data in events)
    assert "private" not in repr(events)


@pytest.mark.parametrize("phase", ["C:/secret/cad.sldprt", "Part1.sldprt", "user input", "", None, 7, {}, "x" * 500])
def test_unknown_phase_is_ignored_not_republished_or_misparsed_as_result(monkeypatch, tmp_path, phase):
    value, events, _, _, _, _ = make_client(monkeypatch, tmp_path, sequence=[{"kind": "progress", "phase": phase}])
    assert value.call(ToolCall("model.observe", {})).ok
    assert [name for name, _ in events] == ["rpc.started", "rpc.finished"]


def test_other_request_progress_not_forwarded(monkeypatch, tmp_path):
    value, events, _, _, _, _ = make_client(monkeypatch, tmp_path, sequence=[{
        "id": "wrong-request", "kind": "progress", "phase": "executing",
    }])
    assert value.call(ToolCall("model.observe", {})).ok
    assert not any(name == "rpc.phase" for name, _ in events)


@pytest.mark.parametrize("kind", ["call", "candidate", "checkpoint", "identity"])
def test_callback_failure_does_not_change_execution_or_repeat_request(monkeypatch, tmp_path, kind):
    def broken(*_):
        raise RuntimeError("UI disconnected")

    value, _, sent, starts, _, process = make_client(monkeypatch, tmp_path, hook=broken,
                                                    sequence=[{"kind": "progress", "phase": "executing"}])
    assert invoke(value, kind) is not None
    assert len(sent) == len(starts) == 1 and process.terminations == 0


@pytest.mark.parametrize("progress", [False, True])
def test_five_second_heartbeats_and_native_phases_never_extend_deadline(monkeypatch, tmp_path, progress):
    def slow(_client, request, clock, timeout):
        clock.now += timeout
        if progress:
            return {"id": request["id"], "kind": "progress", "phase": "executing"}
        raise queue.Empty

    value, events, sent, starts, clock, process = make_client(monkeypatch, tmp_path, on_get=slow)
    with pytest.raises(WorkerError, match="12 秒.*结果未知"):
        value.call(ToolCall("model.observe", {}), 12)
    assert clock.now == 12 and process.terminations == 1
    assert len(sent) == len(starts) == 1
    heartbeats = [data["elapsed_seconds"] for event, data in events if event == "rpc.waiting"]
    assert heartbeats == [5, 10]
    assert {data["phase"] for event, data in events if event == "rpc.waiting"} == {
        "executing" if progress else "waiting_worker",
    }
    assert events[-1][1]["phase"] == "timed_out_unknown"


@pytest.mark.parametrize("kind,seconds", [("call", 120), ("candidate", 180), ("finish", 240),
                                        ("checkpoint", 120), ("restore", 240), ("identity", 30)])
def test_exact_private_deadlines_remain_host_owned(monkeypatch, tmp_path, kind, seconds):
    def slow(_client, _request, clock, timeout):
        clock.now += min(timeout, seconds - clock.now)
        raise queue.Empty

    value, _, sent, starts, clock, process = make_client(monkeypatch, tmp_path, on_get=slow)
    with pytest.raises(WorkerError, match=f"{seconds} 秒"):
        if kind == "finish":
            value.candidate_request("finish", token="token", expected={"complete": True}, accept=True)
        elif kind == "restore":
            value.checkpoint_request("restore", "task", CHECKPOINT_ID)
        else:
            invoke(value, kind)
    assert clock.now == seconds and process.terminations == 1
    assert len(sent) == len(starts) == 1
    if kind in {"candidate", "finish", "checkpoint", "restore"}:
        assert "timeout_seconds" not in sent[0]


def test_lock_wait_is_inside_deadline_and_never_cancels_another_running_call(monkeypatch, tmp_path):
    value, events, sent, starts, clock, process = make_client(monkeypatch, tmp_path)

    class Locked:
        def acquire(self, *, timeout):
            clock.now += timeout
            return False

        def release(self):
            pytest.fail("never acquired")

    value._call_lock = Locked()
    with pytest.raises(WorkerError, match="尚未提交，未执行"):
        value.candidate_request("begin", expected={"complete": True}, task_id="task")
    assert clock.now == 180 and sent == starts == []
    assert process.terminations == 0 and process.running
    assert events[-1][1]["phase"] == "not_executed"


def test_spent_lock_budget_not_reset_when_dispatch_starts(monkeypatch, tmp_path):
    def slow(_client, _request, clock, timeout):
        clock.now += timeout
        raise queue.Empty

    value, _, sent, starts, clock, process = make_client(monkeypatch, tmp_path, on_get=slow)

    class DelayedLock:
        def acquire(self, *, timeout):
            clock.now = 175
            return True

        def release(self):
            pass

    value._call_lock = DelayedLock()
    with pytest.raises(WorkerError, match="180 秒"):
        value.candidate_request("begin", expected={"complete": True}, task_id="task")
    assert clock.now == 180 and process.terminations == 1 and len(sent) == len(starts) == 1


def test_stop_during_real_lock_wait_rejects_without_start_or_submit(monkeypatch, tmp_path):
    value, _, sent, starts, _, _ = make_client(monkeypatch, tmp_path)

    class Lock:
        def acquire(self, *, timeout):
            value.interrupt()
            return False

    value._call_lock = Lock()
    with pytest.raises(WorkerError, match="已被停止"):
        value.checkpoint_request("list", "task")
    assert sent == starts == []


@pytest.mark.parametrize("operation", ["list", "restore"])
def test_checkpoint_request_has_exact_private_fields(monkeypatch, tmp_path, operation):
    value, _, sent, _, _, _ = make_client(monkeypatch, tmp_path, hook=False)
    kwargs = {"checkpoint_id": CHECKPOINT_ID} if operation == "restore" else {}
    assert value.checkpoint_request(operation, "task", **kwargs) == {"ok": True}
    assert set(sent[0]) == {"id", "kind", "operation", "task_id", *kwargs}
    assert sent[0]["kind"] == "checkpoint"
    assert "call" not in sent[0]


@pytest.mark.parametrize("operation,task_id,checkpoint_id", [
    ("delete", "task", None), (None, "task", None), ({}, "task", None),
    ("list", "", None), ("list", None, None), ("list", "a" * 129, None), ("list", "x\0y", None),
    ("list", "task", CHECKPOINT_ID), ("restore", "task", None), ("restore", "task", "../file"),
    ("restore", "task", "A" * 32), ("restore", "task", "a" * 31), ("restore", "task", True),
])
def test_checkpoint_invalid_fields_rejected_before_start(monkeypatch, tmp_path, operation, task_id, checkpoint_id):
    value, _, sent, starts, _, _ = make_client(monkeypatch, tmp_path)
    with pytest.raises(WorkerError):
        value.checkpoint_request(operation, task_id, checkpoint_id)
    assert sent == starts == []


def test_checkpoint_workspace_required_and_unknown_fields_not_accepted(monkeypatch, tmp_path):
    value, _, sent, starts, _, _ = make_client(monkeypatch, tmp_path)
    with pytest.raises(TypeError):
        value.checkpoint_request("list", "task", path="C:/user")
    value._candidate_workspace = None
    with pytest.raises(WorkerError):
        value.checkpoint_request("list", "task")
    assert sent == starts == []


@pytest.mark.parametrize("operation", ["list", "restore"])
def test_cancelled_checkpoint_epoch_never_restarts_or_submits(monkeypatch, tmp_path, operation):
    value, _, sent, starts, _, _ = make_client(monkeypatch, tmp_path)
    with value.operation():
        value.interrupt()
        with pytest.raises(WorkerError, match="已被停止"):
            value.checkpoint_request(operation, "task", CHECKPOINT_ID if operation == "restore" else None)
    assert sent == starts == []


def test_nested_operation_does_not_reset_cancelled_outer_epoch(monkeypatch, tmp_path):
    value, _, sent, starts, _, _ = make_client(monkeypatch, tmp_path)
    with value.operation():
        value.interrupt()
        with value.operation():
            with pytest.raises(WorkerError, match="已被停止"):
                value.checkpoint_request("restore", "task", CHECKPOINT_ID)
    assert sent == starts == []


@pytest.mark.parametrize("operation", ["list", "restore"])
def test_cancelled_checkpoint_late_success_or_phase_discarded(monkeypatch, tmp_path, operation):
    def late(value, request, _clock, _timeout):
        value.interrupt()
        return {"id": request["id"], "kind": "checkpoint", "ok": True, "data": {"ok": True}}

    value, events, sent, starts, _, _ = make_client(monkeypatch, tmp_path, on_get=late)
    with pytest.raises(WorkerError, match="晚到"):
        value.checkpoint_request(operation, "task", CHECKPOINT_ID if operation == "restore" else None)
    assert len(sent) == len(starts) == 1 and events[-1][1]["phase"] != "completed"
    assert events[-1][1]["phase"] == "stopped_unknown"


def test_expired_late_success_is_unknown_not_completed(monkeypatch, tmp_path):
    def late(_value, request, clock, _timeout):
        clock.now = 241
        return {"id": request["id"], "kind": "checkpoint", "ok": True, "data": {"ok": True}}

    value, events, _, _, _, process = make_client(monkeypatch, tmp_path, on_get=late)
    with pytest.raises(WorkerError, match="结果未知"):
        value.checkpoint_request("restore", "task", CHECKPOINT_ID)
    assert process.terminations == 1 and events[-1][1]["phase"] == "timed_out_unknown"


@pytest.mark.parametrize("kind", ["call", "checked_call", "candidate", "checkpoint", "identity"])
def test_cancel_at_final_deadline_read_cannot_overwrite_stop_with_success(monkeypatch, tmp_path, kind):
    value, _, sent, starts, clock, process = make_client(monkeypatch, tmp_path, hook=False)
    original_check = value._check_generation
    armed = {"remaining_reads": None}

    def checked(generation, *, submitted=False):
        original_check(generation, submitted=submitted)
        # The response check is the second submitted-generation check: the
        # following time reads are receipt validation and finalization.
        if submitted:
            checked.count += 1
            if checked.count == 2:
                armed["remaining_reads"] = 2

    checked.count = 0

    def monotonic():
        if armed["remaining_reads"] is not None:
            armed["remaining_reads"] -= 1
            if armed["remaining_reads"] == 0:
                armed["remaining_reads"] = None
                value.interrupt()  # Same-thread reentrancy also exercises RLock ordering.
        return clock.now

    monkeypatch.setattr(value, "_check_generation", checked)
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=monotonic))
    with pytest.raises(WorkerError, match="晚到"):
        invoke(value, kind)
    assert process.terminations == 1 and value._generation == 1
    assert len(sent) == len(starts) == 1


def test_unexpected_process_exit_fences_same_operation_from_restart(monkeypatch, tmp_path):
    def died(_value, _request, _clock, _timeout):
        process.running = False
        raise queue.Empty

    value, _, sent, starts, _, process = make_client(monkeypatch, tmp_path, on_get=died)
    with value.operation():
        with pytest.raises(WorkerError, match="意外退出"):
            value.checkpoint_request("restore", "task", CHECKPOINT_ID)
        with pytest.raises(WorkerError, match="已被停止"):
            value.checkpoint_request("list", "task")
    assert len(sent) == len(starts) == 1


@pytest.mark.parametrize("response", [{"kind": "call", "ok": True, "data": {}},
                                     {"kind": "checkpoint", "ok": 1, "data": {}},
                                     {"kind": "checkpoint", "ok": True, "data": []},
                                     {"kind": "checkpoint", "ok": False, "error": "refused"}])
def test_checkpoint_invalid_response_never_retried(monkeypatch, tmp_path, response):
    value, _, sent, starts, _, _ = make_client(monkeypatch, tmp_path, sequence=[response])
    with pytest.raises(WorkerError):
        value.checkpoint_request("list", "task")
    assert len(sent) == len(starts) == 1
