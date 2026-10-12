from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt

from sw_assistant.agent.task_models import TaskDecision
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.infrastructure import AppPaths
from sw_assistant.runtime import ApplicationController
from sw_assistant.domain import Plan, ToolCall
from sw_assistant.domain.errors import WorkerError


def make_controller(tmp_path):
    return ApplicationController(AppPaths(
        data=tmp_path, logs=tmp_path / "logs", settings=tmp_path / "settings.json",
        library_db=tmp_path / "library.db", history_db=tmp_path / "history.db",
    ))


def wait_idle(service):
    deadline = time.monotonic() + 3
    while service.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not service.busy


def test_prepare_without_model_reports_configuration_error_without_cad(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    calls = []
    monkeypatch.setattr(controller.worker, "call", lambda *args: calls.append(args))
    try:
        controller.continuous.prepare("新建一个零件")
        wait_idle(controller.continuous)
        state = controller.continuous.snapshot()
        assert state["status"] == "failed"
        assert "模型服务" in state["message"]
        assert calls == []
    finally:
        controller.shutdown()


def test_stop_discards_late_initial_decision_and_releases_background_lock(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    entered, release = threading.Event(), threading.Event()
    cad_calls = []
    interrupted = []

    class SlowBackend:
        def decide(self, context):
            entered.set()
            release.wait(3)
            return TaskDecision(action="inspect", reason="检查当前模型")

        def close(self):
            release.set()

    service = controller.continuous
    monkeypatch.setattr(controller.worker, "call", lambda *args: cad_calls.append(args))
    monkeypatch.setattr(controller.worker, "interrupt", lambda: interrupted.append(True))
    monkeypatch.setattr(service, "_new_runner", lambda _session: ContinuousTaskRunner(
        controller.registry, controller.worker, SlowBackend(), service.store,
        on_event=service._event,
    ))
    try:
        service.prepare("新建一个零件")
        assert entered.wait(2)
        assert not controller._claim_background("library", "other")
        controller.abort_all()
        wait_idle(service)
        assert service.snapshot()["status"] == "stopped"
        assert not cad_calls
        assert interrupted
        assert controller._active_background is None
    finally:
        release.set()
        controller.shutdown()


def test_stop_between_backend_creation_and_assignment_cannot_start_model(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    entered, release = threading.Event(), threading.Event()
    decisions = []

    class Backend:
        def decide(self, context):
            decisions.append(context)
            return TaskDecision(action="inspect", reason="检查")

        def close(self):
            pass

    service = controller.continuous

    def factory(_session):
        entered.set()
        release.wait(3)
        return ContinuousTaskRunner(controller.registry, controller.worker, Backend(), service.store)

    monkeypatch.setattr(service, "_new_runner", factory)
    try:
        service.prepare("新建零件")
        assert entered.wait(2)
        service.stop()
        release.set()
        wait_idle(service)
        assert decisions == []
        assert controller._active_background is None
    finally:
        release.set()
        controller.shutdown()


def test_agent_event_named_event_is_recorded_without_keyword_collision(tmp_path):
    controller = make_controller(tmp_path)
    received = []
    controller.continuous.activity.connect(
        lambda name, payload: received.append((name, payload)), Qt.ConnectionType.DirectConnection
    )
    try:
        controller.continuous._event("pi_event", {"event": "message_update"})
        assert received[-1] == ("pi_event", {"event": "message_update"})
    finally:
        controller.shutdown()


def test_stop_invalidates_queued_legacy_load_run_and_selection(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    entered, release = threading.Event(), threading.Event()
    runs, captures = [], []

    def blocked_run():
        runs.append(True)
        entered.set()
        release.wait(3)

    monkeypatch.setattr(controller.engine, "run_all", blocked_run)
    monkeypatch.setattr(controller, "_capture_selection_reference", captures.append)
    try:
        controller.run_all()
        assert entered.wait(2)
        controller._commands.put(("load", Plan("old queued", [ToolCall("session.ensure", {})])))
        controller._commands.put(("all", None))
        controller._commands.put(("capture_selection_reference", "old selection"))
        controller.abort_all()
        release.set()
        deadline = time.monotonic() + 2
        while controller._active_background is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        # A marker load enqueued after Stop must still work, proving the queue drained.
        fresh = Plan("fresh", [ToolCall("session.ensure", {})])
        controller.load_plan(fresh)
        deadline = time.monotonic() + 2
        while controller.engine.plan is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller.engine.plan is not None and controller.engine.plan.id == fresh.id
        assert len(runs) == 1
        assert captures == []
    finally:
        release.set()
        controller.shutdown()


def test_stopped_operation_cannot_restart_worker_after_late_model_reply(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    started = []
    monkeypatch.setattr(controller.worker, "start", lambda: started.append(True))
    try:
        with controller.worker.operation():
            controller.worker.interrupt()
            try:
                controller.worker.call(ToolCall("session.ensure", {}))
            except WorkerError as exc:
                assert "已被停止" in str(exc)
            else:
                raise AssertionError("cancelled scope was allowed to restart a worker")
        assert started == []
    finally:
        controller.shutdown()
