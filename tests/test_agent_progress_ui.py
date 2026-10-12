"""Public progress survives activity -> snapshot Qt repaint, without CoT."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from sw_assistant.ui.task_panel import TaskPanel
from test_task_runtime import make_controller
from test_workbench_runtime import StubRunner


def response_event(task_id="stub", count=8):
    return {"task_id": task_id, "message": f"模型已返回第 {count} 次响应，正在形成阶段决策。",
            "progress": {"decision_rounds": 1, "observed_model_responses": count,
                         "round_investigation_requests": 8, "thinking": "PRIVATE-COUNTER"},
            "raw_reasoning": "PRIVATE-REASONING"}


def test_public_response_count_survives_real_service_signal_order_and_housekeeping(qapp, tmp_path):
    owner = make_controller(tmp_path)
    service = owner.continuous
    runner = StubRunner(owner)
    runner.state.update(status="running", phase="需求分析", message="正在分析需求。")
    service.runner = runner
    panel = TaskPanel(service)
    try:
        service.updated.emit(service.snapshot())
        service._event("agent_response_progress", response_event())
        qapp.processEvents()
        assert "第 8 次响应" in panel.phase_label.text()
        assert "本轮已调查 8 次" in panel.phase_label.text()
        service._event("pi_event", {"task_id": "stub", "message": "Pi 会话处理中", "event": "message_update"})
        qapp.processEvents()
        assert "第 8 次响应" in panel.phase_label.text()
        assert "第 8 次响应" in panel.log.toPlainText()
        assert "PRIVATE" not in panel.log.toPlainText() + panel.phase_label.text()
        assert service.snapshot()["agent_progress"] == {
            "decision_rounds": 1, "observed_model_responses": 8, "round_investigation_requests": 8,
        }
        runner.backend = SimpleNamespace(progress={
            "decision_rounds": 2, "observed_model_responses": 9, "round_investigation_requests": 1,
        })
        service._event("agent_investigation_finished", {"task_id": "stub", "message": "已取得调查结果"})
        assert "本轮已调查 1 次" in panel.phase_label.text()
        assert "第 9 次响应" in panel.phase_label.text()
        service._event("stage_started", {"message": "开始建模阶段"})
        assert service.snapshot()["agent_progress_message"] == ""
    finally:
        panel.close()
        owner.shutdown()


@pytest.mark.parametrize("status", ["review", "completed", "stopped", "failed", "interrupted"])
def test_terminal_blocker_wins_over_late_progress(qapp, tmp_path, status):
    owner = make_controller(tmp_path)
    service = owner.continuous
    runner = StubRunner(owner)
    runner.state.update(status="running", message="分析中")
    service.runner = runner
    panel = TaskPanel(service)
    try:
        service.updated.emit(service.snapshot())
        service._event("agent_response_progress", response_event())
        runner.state.update(status=status, message="现场核对结果或明确完成信息")
        service._event("agent_response_progress", response_event(count=9))
        qapp.processEvents()
        assert "现场核对结果或明确完成信息" in panel.phase_label.text()
        assert "第 9 次响应" not in panel.phase_label.text()
        assert service.snapshot()["agent_progress_message"] == ""
        assert service.snapshot()["agent_progress"] == {}
    finally:
        panel.close()
        owner.shutdown()


def test_stop_and_replacement_clear_progress_and_old_task_cannot_restore_it(qapp, tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    service = owner.continuous
    runner = StubRunner(owner)
    runner.state["status"] = "running"
    service.runner = runner
    try:
        service._event("agent_response_progress", response_event())
        assert service.snapshot()["agent_progress_message"]
        service.stop()
        runner.state["status"] = "running"  # late backend event during stop completion
        service._event("agent_response_progress", response_event(count=9))
        assert service.snapshot()["agent_progress_message"] == ""
        new = StubRunner(owner, "new")
        new.state["status"] = "running"
        monkeypatch.setattr(service, "_new_runner", lambda _id: new)
        service._replace_runner("new", service._generation)
        service._event("agent_response_progress", response_event())
        assert service.snapshot()["id"] == "new"
        assert service.snapshot()["agent_progress_message"] == ""
    finally:
        owner.shutdown()


def test_progress_counter_fields_are_typed_and_bounded(qapp, tmp_path):
    owner = make_controller(tmp_path)
    service = owner.continuous
    runner = StubRunner(owner)
    runner.state["status"] = "running"
    service.runner = runner
    try:
        data = response_event()
        data["progress"] = {"decision_rounds": -1, "observed_model_responses": "PRIVATE",
                            "round_investigation_requests": True}
        service._event("agent_response_progress", data)
        assert service.snapshot()["agent_progress"] == {}
        assert service.snapshot()["agent_progress_message"] == "模型已返回新的响应；正在形成阶段决策。"
    finally:
        owner.shutdown()


def test_output_truncation_is_not_shown_as_repeated_investigation(qapp, tmp_path):
    owner = make_controller(tmp_path)
    service = owner.continuous
    runner = StubRunner(owner)
    runner.state["status"] = "running"
    service.runner = runner
    panel = TaskPanel(service)
    try:
        service.updated.emit(service.snapshot())
        service._event("agent_loop_correction", {"task_id": "stub", "correction_kind": "output_truncated",
                                                "message": "响应输出截断", "output_tokens": 8192})
        assert "输出长度上限" in panel.phase_label.text()
        assert "已完成步骤" in panel.phase_label.text()
        assert "重复调查" not in panel.phase_label.text()
        service._event("agent_loop_correction", {"task_id": "stub", "correction_kind": "investigation_handoff",
                                                "message": "调查已结束"})
        assert "本轮调查已结束" in panel.phase_label.text()
        assert "等待下一阶段决策" in panel.phase_label.text()
        assert "无效候选" not in panel.phase_label.text()
    finally:
        panel.close()
        owner.shutdown()
