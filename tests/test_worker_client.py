from __future__ import annotations

import queue
import threading
import time

from sw_assistant.domain import ToolCall
from sw_assistant.solidworks.client import SolidWorksWorkerClient


class _Process:
    def __init__(self) -> None:
        self.running = True

    def is_alive(self) -> bool:
        return self.running

    def terminate(self) -> None:
        self.running = False

    def join(self, timeout=None) -> None:
        return None


class _RequestQueue:
    def __init__(self, submitted: threading.Event) -> None:
        self.submitted = submitted

    def put(self, _value) -> None:
        self.submitted.set()


class _ResponseQueue:
    def get(self, timeout=None):
        time.sleep(min(float(timeout or 0), 0.02))
        raise queue.Empty


def test_interrupt_unblocks_active_worker_call(monkeypatch):
    client = SolidWorksWorkerClient()
    submitted = threading.Event()
    process = _Process()
    client._process = process
    client._requests = _RequestQueue(submitted)
    client._responses = _ResponseQueue()
    monkeypatch.setattr(client, "start", lambda: None)
    outcome = []

    thread = threading.Thread(
        target=lambda: outcome.append(_capture_error(client)),
        daemon=True,
    )
    thread.start()
    assert submitted.wait(1)
    client.interrupt()
    thread.join(1)
    assert not thread.is_alive()
    assert "已停止" in outcome[0]


def _capture_error(client: SolidWorksWorkerClient) -> str:
    try:
        client.call(ToolCall("model.inspect", {}), 10)
    except Exception as exc:
        return str(exc)
    return ""
