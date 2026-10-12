"""Host precondition transport and cancellation, with no CAD or network."""
from __future__ import annotations

import queue
import threading

import pytest

from sw_assistant.domain import StepResult, ToolCall
from sw_assistant.domain.errors import WorkerError
from sw_assistant.solidworks.client import SolidWorksWorkerClient


class Process:
    def is_alive(self):
        return True

    def terminate(self):
        pass

    def join(self, timeout=None):
        pass


def make_client(monkeypatch, *, on_get=None):
    value = SolidWorksWorkerClient()
    value._process = Process()
    value._requests = queue.Queue()
    sent, starts = [], []

    class Responses:
        def get(self, timeout=None):
            request = value._requests.get_nowait()
            sent.append(request)
            if on_get:
                on_get(value, request)
            return {"id": request["id"], "result": StepResult(
                True, ToolCall.from_mapping(request["call"]), "synthetic success"
            ).to_dict()}

    value._responses = Responses()
    monkeypatch.setattr(value, "start", lambda: starts.append(True))
    return value, sent, starts


def test_checked_request_keeps_expected_outside_model_tool_arguments(monkeypatch):
    expected = {"complete": True, "document_identity": "synthetic:part", "snapshot": {"body_count": 1}}
    value, sent, starts = make_client(monkeypatch)
    command = ToolCall("sketch.rectangle", {"width_mm": 12, "height_mm": 20})
    assert value.call_checked(command, expected).ok
    assert starts == [True]
    assert set(sent[0]) == {"kind", "id", "call", "expected"}
    assert sent[0]["kind"] == "checked_call"
    assert sent[0]["expected"] == expected
    assert sent[0]["call"] == command.to_dict()
    expected["snapshot"]["body_count"] = 99
    assert sent[0]["expected"]["snapshot"]["body_count"] == 1


@pytest.mark.parametrize("expected", [None, {}, [], {"complete": False}, {"complete": 1}])
def test_incomplete_checked_request_never_starts_worker(monkeypatch, expected):
    value, sent, starts = make_client(monkeypatch)
    with pytest.raises(WorkerError):
        value.call_checked(ToolCall("sketch.circle", {"radius_mm": 3}), expected)
    assert sent == starts == []


def test_bootstrap_call_preserves_ordinary_request_contract(monkeypatch):
    value, sent, _ = make_client(monkeypatch)
    assert value.call(ToolCall("session.ensure", {})).ok
    assert set(sent[0]) == {"kind", "id", "call"}
    assert sent[0]["kind"] == "call"


@pytest.mark.parametrize("checked", [False, True])
def test_stop_during_response_discards_late_success(monkeypatch, checked):
    value, sent, _ = make_client(monkeypatch, on_get=lambda client, _: client.interrupt())
    with pytest.raises(WorkerError, match="晚到"):
        if checked:
            value.call_checked(ToolCall("sketch.circle", {"radius_mm": 3}), {"complete": True})
        else:
            value.call(ToolCall("model.observe", {}))
    assert len(sent) == 1


@pytest.mark.parametrize("checked", [False, True])
def test_stop_while_waiting_for_call_lock_cannot_restart_worker(monkeypatch, checked):
    value, sent, starts = make_client(monkeypatch)
    waiting, release = threading.Event(), threading.Event()

    class BlockedLock:
        def __enter__(self):
            waiting.set()
            assert release.wait(3)

        def __exit__(self, *_):
            pass

    value._call_lock = BlockedLock()
    errors = []

    def run():
        try:
            if checked:
                value.call_checked(ToolCall("sketch.circle", {"radius_mm": 3}), {"complete": True})
            else:
                value.call(ToolCall("model.observe", {}))
        except WorkerError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run)
    thread.start()
    assert waiting.wait(3)
    value.interrupt()
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1 and "已被停止" in errors[0]
    assert sent == starts == []
