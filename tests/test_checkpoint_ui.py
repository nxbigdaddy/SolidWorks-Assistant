from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from PySide6.QtCore import Qt

from sw_assistant.ui.task_panel import TaskPanel
from sw_assistant.infrastructure import ModelSettings
from test_offline_checkpoint_runtime import offline_controller
from test_task_panel import FakeContinuous, _snapshot
from test_task_runtime import make_controller, wait_idle

KEY = "a" * 32


class CheckpointService(FakeContinuous):
    def refresh_checkpoints(self):
        self.calls.append(("refresh_checkpoints",))

    def restore_checkpoint(self, checkpoint_id):
        self.calls.append(("restore_checkpoint", checkpoint_id))


def catalog(available=True):
    return [{"checkpoint_id": KEY, "integrity": "verified", "host_prefix_available": available,
             "created_at": "2026-09-08T09:00:00", "completed_count": 5}]


def test_checkpoint_ui_requires_selection_and_does_not_auto_continue(qapp):
    service = CheckpointService()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("review", checkpoint_catalog=catalog()))
        assert panel.checkpoint_refresh_button.isEnabled()
        assert not panel.checkpoint_restore_button.isEnabled()
        panel.checkpoints.setCurrentIndex(1)
        assert panel.checkpoint_restore_button.isEnabled()
        panel.checkpoint_restore_button.click()
        panel.checkpoint_restore_button.click()
        assert service.calls == [("restore_checkpoint", KEY)]
        assert panel.stop_button.isEnabled()
        service.updated.emit(_snapshot("ready", checkpoint_catalog=catalog(), pending_steps=[]))
        assert service.calls == [("restore_checkpoint", KEY)]
        assert panel.run_button.isEnabled()
    finally:
        panel.close()


def test_missing_host_prefix_and_busy_disable_restore(qapp):
    service = CheckpointService()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("review", checkpoint_catalog=catalog(False)))
        panel.checkpoints.setCurrentIndex(1)
        assert not panel.checkpoint_restore_button.isEnabled()
        service.updated.emit(_snapshot("running", checkpoint_catalog=catalog()))
        assert not panel.checkpoint_restore_button.isEnabled()
        assert not panel.checkpoint_refresh_button.isEnabled()
        assert panel.stop_button.isEnabled()
    finally:
        panel.close()


def test_list_is_explicit_and_new_task_clears_old_checkpoint_selection(qapp):
    service = CheckpointService()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("review", checkpoint_catalog=catalog()))
        assert service.calls == []
        panel.checkpoint_refresh_button.click()
        assert service.calls == [("refresh_checkpoints",)]
        service.updated.emit(_snapshot("review", checkpoint_catalog=catalog()))
        panel.checkpoints.setCurrentIndex(1)
        panel.new_draft()
        assert panel.checkpoints.count() == 1
        assert not panel.checkpoint_restore_button.isEnabled()
    finally:
        panel.close()


def test_late_native_progress_does_not_replace_stopped_ui(qapp):
    service = CheckpointService()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("stopped", message="用户已停止"))
        phase, log = panel.phase_label.text(), panel.log.toPlainText()
        service.activity.emit("cad_progress", {"message": "迟到的保存状态", "phase": "saving"})
        assert panel.phase_label.text() == phase
        assert panel.log.toPlainText() == log
    finally:
        panel.close()


def test_controller_native_progress_filters_payload_and_drops_after_stop(tmp_path):
    controller = make_controller(tmp_path)
    service = controller.continuous
    state = {"id": "task", "status": "running"}
    events = []
    service.runner = SimpleNamespace(snapshot=lambda: dict(state), stop=lambda: None)
    service._busy = True
    service.activity.connect(lambda name, payload: events.append((name, payload)), Qt.ConnectionType.DirectConnection)
    try:
        service._event("cad_progress", {"message": "正在保存检查点", "phase": "saving",
                                        "elapsed_seconds": 12, "arguments": {"private": "not logged"}})
        assert service.snapshot()["agent_progress_message"] == "正在保存检查点 · 已用 12 秒"
        assert "arguments" not in events[-1][1]
        service.stop()
        service._event("cad_progress", {"message": "late"})
        assert len(events) == 1
        assert service.snapshot()["agent_progress_message"] == ""
    finally:
        service._busy = False
        controller.shutdown()


def test_controller_checkpoint_command_does_not_run_or_resume(tmp_path):
    controller = make_controller(tmp_path)
    service = controller.continuous
    state = {"id": "task", "status": "review"}
    calls = []

    def restore(key):
        calls.append(("restore_checkpoint", key))
        state["status"] = "ready"

    service.runner = SimpleNamespace(
        snapshot=lambda: dict(state), stop=lambda: None, restore_checkpoint=restore,
        refresh_checkpoints=lambda: calls.append(("refresh_checkpoints",)),
        run=lambda: calls.append(("run",)),
    )
    try:
        service.refresh_checkpoints()
        wait_idle(service)
        service.restore_checkpoint(KEY)
        wait_idle(service)
        assert calls == [("refresh_checkpoints",), ("restore_checkpoint", KEY)]
        assert service.snapshot()["status"] == "ready"
    finally:
        controller.shutdown()


@pytest.mark.parametrize("operation", ["run", "step"])
def test_first_slow_resume_observation_reports_progress_while_ready_and_stop_wins(tmp_path, monkeypatch, qapp, operation):
    controller, worker, model_calls, task_id = offline_controller(tmp_path, monkeypatch)
    controller.settings.model = ModelSettings(api_base="http://127.0.0.1:9/v1", model="local-fixture")
    service = controller.continuous
    entered, release = threading.Event(), threading.Event()
    panel = TaskPanel(service)
    try:
        service.restore(task_id)
        wait_idle(service)
        runner = service.runner
        assert runner is not None
        runner._task.update(status="ready", active_trial=None)

        def slow_observation(command, timeout_seconds=120):
            assert command.tool == "model.observe"
            assert runner.snapshot()["status"] == "ready"
            service._event("cad_progress", {"phase": "observe", "message": "正在读取继续前的模型状态", "elapsed_seconds": 14})
            entered.set()
            release.wait(3)
            service._event("cad_progress", {"phase": "observe", "message": "停止后的迟到进度", "elapsed_seconds": 15})
            return worker.call(command, timeout_seconds)

        monkeypatch.setattr(controller.worker, "call", slow_observation)
        getattr(service, operation)()
        assert entered.wait(2)
        qapp.processEvents()
        state = service.snapshot()
        assert state["status"] == "ready" and state["busy"]
        assert "继续前的模型状态" in state["agent_progress_message"]
        assert "已用 14 秒" in panel.phase_label.text()
        assert panel.stop_button.isEnabled()
        service.stop()
        qapp.processEvents()
        assert service.snapshot()["status"] == "stopped"
        assert service.snapshot()["agent_progress_message"] == ""
        release.set()
        wait_idle(service)
        qapp.processEvents()
        assert service.snapshot()["status"] == "stopped"
        assert "迟到进度" not in panel.log.toPlainText()
        assert "迟到进度" not in panel.phase_label.text()
        assert model_calls == []
    finally:
        release.set()
        panel.close()
        controller.shutdown()


@pytest.mark.parametrize("status,busy", [("ready", False), ("review", True), ("stopped", True),
                                        ("completed", True), ("failed", True), ("interrupted", True)])
def test_native_progress_does_not_promote_nonexecuting_states(tmp_path, qapp, status, busy):
    controller = make_controller(tmp_path)
    service = controller.continuous
    state = {"id": "task", "status": status, "message": "保持原有状态"}
    service.runner = SimpleNamespace(snapshot=lambda: dict(state), stop=lambda: None)
    service._busy = busy
    panel = TaskPanel(service)
    events = []
    service.activity.connect(lambda name, payload: events.append(name), Qt.ConnectionType.DirectConnection)
    try:
        panel.render_snapshot({**state, "busy": busy})
        phase = panel.phase_label.text()
        service._event("cad_progress", {"message": "不应该展示的迟到进度", "elapsed_seconds": 20})
        assert service.snapshot()["status"] == status
        assert service.snapshot()["agent_progress_message"] == ""
        assert events == []
        panel.on_activity("cad_progress", {"message": "不应该展示的迟到进度"})
        assert panel.phase_label.text() == phase
    finally:
        service._busy = False
        panel.close()
        controller.shutdown()
