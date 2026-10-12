from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest

from sw_assistant.ui.task_panel import TaskPanel


class FakeContinuous(QObject):
    updated = Signal(object)
    activity = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.records = []

    def prepare(self, prompt):
        self.calls.append(("prepare", prompt))

    def start(self, prompt):
        self.calls.append(("start", prompt))

    def steer(self, note):
        self.calls.append(("steer", note))

    def run(self):
        self.calls.append(("run",))

    def step(self):
        self.calls.append(("step",))

    def resume(self, note):
        self.calls.append(("resume", note))

    def resume_and_run(self, note):
        self.calls.append(("resume_and_run", note))

    def resume_requirements_and_run(self, note):
        self.calls.append(("resume_requirements_and_run", note))

    def restore(self, task_id):
        self.calls.append(("restore", task_id))

    def stop(self):
        self.calls.append(("stop",))

    def recent(self):
        return list(self.records)


def _snapshot(status="ready", **values):
    return {
        "id": "one",
        "prompt": "圆柱与通孔",
        "status": status,
        "phase": "ready",
        "message": "等待执行首个阶段。",
        "rounds": 1,
        "goals": ["主体直径 40 mm", "通孔直径 10 mm"],
        "assumptions": ["未指定材料"],
        "pending_steps": [{"tool": "document.new_part", "description": "新建零件"}],
        "completed_steps": [],
        "events": [],
        "verification": [],
        **values,
    }


def test_prepare_locks_immediately_and_does_not_execute(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        panel.prompt.setPlainText("圆柱与通孔")
        panel.prepare_button.click()
        panel.prepare_button.click()
        panel.run_button.click()
        assert service.calls == [("prepare", "圆柱与通孔")]
        assert not panel.prepare_button.isEnabled()
        assert panel.prompt.isReadOnly()
        assert panel.progress.maximum() == 0
        assert panel.stop_button.isEnabled()
        service.updated.emit(_snapshot())
        assert panel.run_button.isEnabled()
        assert panel.step_button.isEnabled()
        assert panel.progress.maximum() == 1
        assert "主体直径 40 mm" in panel.checklist.toPlainText()
        assert panel.steps.item(0, 1).text() == "新建零件"
    finally:
        panel.close()


def test_running_prevents_duplicate_and_stop_reaches_global_callback(qapp):
    service = FakeContinuous()
    stopped = []
    panel = TaskPanel(service, stop_all=lambda: stopped.append("all"))
    try:
        service.updated.emit(_snapshot())
        panel.run_button.click()
        panel.step_button.click()
        panel.run_button.click()
        assert service.calls == [("run",)]
        panel.stop_button.click()
        assert stopped == ["all"]
        assert ("stop",) not in service.calls
        service.updated.emit(_snapshot("stopped", message="任务已停止，请先核对模型。"))
        assert not panel.run_button.isEnabled()
        assert not panel.review_card.isHidden()
    finally:
        panel.close()


def test_review_requires_feedback_and_resumes_through_one_service_operation(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("review", message="请退出草图并检查顶面。"))
        assert "检查顶面" in panel.review_message.text()
        assert not panel.feedback_requirements.isChecked()
        panel.resume_button.click()
        assert service.calls == []
        panel.feedback.setPlainText("已退出草图，重新选择顶面")
        panel.resume_button.click()
        panel.resume_button.click()
        assert service.calls == [("resume_and_run", "已退出草图，重新选择顶面")]
        assert panel.prompt.toPlainText() == ""
        service.updated.emit(_snapshot())
        assert panel.review_card.isHidden()
        assert panel.run_button.isEnabled()
        assert ("run",) not in service.calls
    finally:
        panel.close()


def test_review_new_requirements_uses_distinct_resume_and_disables_while_busy(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        panel.show()
        service.updated.emit(_snapshot("review"))
        assert panel.feedback_requirements.isVisible()
        assert panel.feedback_requirements.isEnabled()
        QTest.mouseClick(panel.feedback_requirements, Qt.MouseButton.LeftButton)
        panel.feedback.setPlainText("新增两个直径 6 mm 的安装孔")
        QTest.mouseClick(panel.resume_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(panel.resume_button, Qt.MouseButton.LeftButton)
        assert service.calls == [("resume_requirements_and_run", "新增两个直径 6 mm 的安装孔")]
        assert not panel.feedback_requirements.isEnabled()
        assert not panel.feedback_requirements.isChecked()
        assert panel.prompt.toPlainText() == ""
        assert panel.stop_button.isEnabled()
        service.updated.emit(_snapshot("running"))
        assert panel.feedback_requirements.isHidden()
        assert not panel.feedback_requirements.isEnabled()
    finally:
        panel.close()


def test_feedback_requirement_choice_resets_for_new_or_restored_tasks(qapp):
    service = FakeContinuous()
    service.records = [_snapshot("review", id="saved")]
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("review"))
        panel.feedback_requirements.setChecked(True)
        panel.new_draft()
        assert not panel.feedback_requirements.isChecked()
        assert panel.feedback_requirements.isHidden()
        service.updated.emit(_snapshot("review"))
        panel.feedback_requirements.setChecked(True)
        service.updated.emit(_snapshot("review", id="another"))
        assert not panel.feedback_requirements.isChecked()
        panel.feedback_requirements.setChecked(True)
        panel.recent_tasks.setCurrentIndex(1)
        panel.restore_button.click()
        assert service.calls == [("restore", "saved")]
        assert not panel.feedback_requirements.isChecked()
        assert not panel.feedback_requirements.isEnabled()
        service.updated.emit(_snapshot("review", id="saved"))
        assert not panel.feedback_requirements.isChecked()
        assert panel.feedback_requirements.isEnabled()
        service.updated.emit(_snapshot("stopped", id="saved"))
        assert panel.feedback_requirements.isHidden()
    finally:
        panel.close()


def test_restoring_interrupted_history_never_runs_and_preserves_record(qapp):
    service = FakeContinuous()
    record = _snapshot("interrupted", id="saved", prompt="历史未完成的支架")
    service.records = [record]
    panel = TaskPanel(service)
    try:
        panel.recent_tasks.setCurrentIndex(1)
        panel.restore_button.click()
        assert service.calls == [("restore", "saved")]
        assert not panel.restore_button.isEnabled()
        service.updated.emit(record)
        assert panel.prompt.toPlainText() == ""
        assert "历史未完成的支架" in panel.log.toPlainText()
        assert not panel.run_button.isEnabled()
        assert not panel.review_card.isHidden()
    finally:
        panel.close()


def test_phase_results_and_acceptance_are_separate_from_model_text(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(
            _snapshot(
                "completed",
                phase="completed",
                message="直径与高度检查通过。",
                rounds=4,
                pending_steps=[],
                completed_steps=[
                    {"call": {"tool": "feature.extrude", "description": "拉伸主体"},
                     "message": "已回读深度 30 mm"}
                ],
                verification={"checks": [{"name": "主体尺寸", "passed": True}]},
                events=[{"message": "已核对主体尺寸", "raw_reasoning": "PRIVATE"}],
            )
        )
        assert panel.rounds_label.text() == "第 4 轮"
        assert panel.steps.item(0, 0).text() == "已验证"
        assert "主体尺寸：通过" in panel.checklist.toPlainText()
        assert "已核对主体尺寸" in panel.log.toPlainText()
        assert "PRIVATE" not in panel.log.toPlainText()
        service.activity.emit("thinking", {"raw_reasoning": "PRIVATE"})
        assert "PRIVATE" not in panel.log.toPlainText()
        assert not panel.run_button.isEnabled()
    finally:
        panel.close()


def test_sync_service_failure_unlocks_form_without_losing_request(qapp):
    service = FakeContinuous()

    def fail(_prompt):
        raise RuntimeError("模型服务未配置")

    service.prepare = fail
    panel = TaskPanel(service)
    try:
        panel.prompt.setPlainText("画一个圆柱")
        panel.prepare_button.click()
        assert panel.prepare_button.isEnabled()
        assert panel.prompt.toPlainText() == "画一个圆柱"
        assert "模型服务未配置" in panel.phase_label.text()
        assert panel.progress.maximum() == 1
    finally:
        panel.close()


def test_runtime_acceptance_and_action_events_render_as_readable_results(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(
            _snapshot(
                "review",
                questions=["请确认通孔方向。"],
                verification={
                    "passed": False,
                    "goals": [{"description": "圆柱高度", "expected": 30, "actual": 30,
                               "observable": True, "passed": True}],
                    "implicit": [{"id": "no_feature_errors", "passed": False,
                                  "observable": False}],
                },
                events=[{"name": "step_succeeded", "call": {"description": "拉伸主体"},
                         "result": {"message": "回读深度为 30 mm"}}],
            )
        )
        assert "请确认通孔方向" in panel.review_requests.text()
        text = panel.checklist.toPlainText()
        assert "圆柱高度：通过（目标 30，实际 30）" in text
        assert "特征无重建错误：无法可靠读取" in text
        assert "已验证：拉伸主体；回读深度为 30 mm" in panel.log.toPlainText()
        service.updated.emit({"status": "failed", "message": "模型未配置"})
        assert panel.review_card.isHidden()
        assert panel.send_button.text() == "发送并开始"
    finally:
        panel.close()


def test_main_window_defaults_to_continuous_with_legacy_page_preserved(qapp, tmp_path):
    from sw_assistant.infrastructure import AppPaths
    from sw_assistant.runtime import ApplicationController
    from sw_assistant.ui.main_window import MainWindow

    paths = AppPaths(
        data=tmp_path, logs=tmp_path / "logs", settings=tmp_path / "settings.json",
        library_db=tmp_path / "library.db", history_db=tmp_path / "history.db",
    )
    controller = ApplicationController(paths)
    window = MainWindow(controller)
    try:
        assert window.pages.count() == 4
        assert window.model_pages.currentWidget() is window.task_panel
        window.legacy_plan_action.trigger()
        assert window.model_pages.currentIndex() == 1
        assert window.plan_table is not None
        window._navigate(0)
        assert window.model_pages.currentWidget() is window.task_panel
    finally:
        window.close()


def test_supplement_must_be_sent_before_manually_continuing_ready_task(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot())
        panel.prompt.setPlainText("改成高度 80 mm 的圆柱")
        assert not panel.run_button.isEnabled()
        assert not panel.step_button.isEnabled()
        panel.run_button.click()
        assert service.calls == []
        panel.send_button.click()
        assert service.calls == [("steer", "改成高度 80 mm 的圆柱")]
        assert panel.prompt.toPlainText() == ""
        assert panel.run_button.isEnabled()
    finally:
        panel.close()


def test_default_send_starts_whole_task_once_and_keeps_advanced_optional(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        panel.show()
        panel.prompt.setPlainText("创建一个带加强筋的支架")
        assert panel.send_button.text() == "发送并开始"
        assert panel.advanced_card.isHidden()
        QTest.mouseClick(panel.send_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(panel.send_button, Qt.MouseButton.LeftButton)
        assert service.calls == [("start", "创建一个带加强筋的支架")]
        assert panel.progress.maximum() == 0
        assert panel.stop_button.isEnabled()
        service.updated.emit(_snapshot("running", prompt="创建一个带加强筋的支架"))
        assert panel.prompt.toPlainText() == ""
        assert not panel.prompt.isReadOnly()
        assert "创建一个带加强筋的支架" in panel.log.toPlainText()
    finally:
        panel.close()


def test_running_accepts_keyboard_supplement_without_starting_another_task(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        panel.show()
        service.updated.emit(_snapshot("running"))
        panel.prompt.setFocus()
        QTest.keyClicks(panel.prompt, "keep wall thickness 3 mm")
        service.updated.emit(_snapshot("running", rounds=3, message="正在检查主体"))
        assert panel.prompt.toPlainText() == "keep wall thickness 3 mm"
        assert panel.send_button.isEnabled()
        QTest.mouseClick(panel.send_button, Qt.MouseButton.LeftButton)
        assert service.calls == [("steer", "keep wall thickness 3 mm")]
        assert panel.prompt.toPlainText() == ""
        assert "keep wall thickness 3 mm" in panel.log.toPlainText()
        assert "下个安全边界" in panel.log.toPlainText()
        assert not panel.run_button.isEnabled()
        assert panel.stop_button.isEnabled()
    finally:
        panel.close()


def test_conversation_review_shows_investigations_and_missing_requirement_evidence(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot(
            "review", message="已找到候选面，但当前模型身份无法确认。",
            questions=["请切回本任务的零件，并确认没有未保存的人工改动。"],
            requirements=[
                {"description": "主体尺寸", "status": "verified", "evidence": ["已回读尺寸"]},
                {"description": "安装孔", "status": "pending"},
                {"description": "加强筋", "status": "completed"},
            ],
            diagnostic_history=[
                {"tool": "model.capabilities", "summary": "支持拉伸和切除"},
                {"tool": "model.faces", "result": {"summary": "找到两个平面候选"}},
            ],
            messages=[
                {"role": "user", "content": "保持孔径为 6 mm"},
                {"role": "assistant", "content": "正在核对可用能力和目标面。"},
                {"role": "analysis", "content": "PRIVATE CHAIN"},
            ],
        ))
        assert "2 项尚缺完成证据" in panel.remaining_label.text()
        assert "安装孔" in panel.checklist.toPlainText()
        assert "尚无完成证据" in panel.checklist.toPlainText()
        investigation = panel.investigation_details.toPlainText()
        assert "支持拉伸和切除" in investigation
        assert "找到两个平面候选" in investigation
        assert "请切回本任务" in panel.review_requests.text()
        assert "保持孔径为 6 mm" in panel.log.toPlainText()
        assert "PRIVATE CHAIN" not in panel.log.toPlainText()
        assert panel.feedback is panel.prompt
        assert panel.send_button.text() == "反馈并继续"
    finally:
        panel.close()


def test_rejected_supplement_preserves_text_and_does_not_claim_queued(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("running"))

        def reject(_note):
            service.activity.emit("task_rejected", {"message": "任务已经停止，未接受补充。"})

        service.steer = reject
        panel.prompt.setPlainText("保留当前轮廓")
        panel.send_button.click()
        assert panel.prompt.toPlainText() == "保留当前轮廓"
        assert "未接受补充" in panel.phase_label.text()
        assert "保留当前轮廓" not in panel.log.toPlainText()
    finally:
        panel.close()


def test_runtime_goal_links_expose_pending_binding_and_verified_child_evidence(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    requirements = [
        {"id": "r1", "description": "带安装孔的支架", "goal_ids": [], "parent_id": ""},
        {"id": "r2", "description": "安装孔直径", "goal_ids": ["hole"], "parent_id": "r1"},
    ]
    try:
        service.updated.emit(_snapshot(
            "running", requirements=requirements,
            goals=[{"id": "hole", "description": "安装孔直径", "expected": 6, "binding": "pending"}],
        ))
        assert "2 项尚缺完成证据" in panel.remaining_label.text()
        assert "验收对象尚待绑定" in panel.checklist.toPlainText()
        service.updated.emit(_snapshot(
            "completed", requirements=requirements,
            verification={"passed": True, "goals": [{"id": "hole", "description": "安装孔直径",
                "passed": True, "observable": True, "expected": 6, "actual": 6}]},
            events=[{"name": "investigation_completed", "result": {"tool": "model.observe",
                     "message": "已读取孔尺寸", "data": {"raw_reasoning": "PRIVATE"}}}],
        ))
        assert "0 项尚缺完成证据" in panel.remaining_label.text()
        assert "↳ 安装孔直径" in panel.checklist.toPlainText()
        assert "期望 6，读取 6" in panel.checklist.toPlainText()
        assert "已读取孔尺寸" in panel.log.toPlainText()
        assert "PRIVATE" not in panel.log.toPlainText()
    finally:
        panel.close()


def test_actual_engine_fallback_and_response_idle_budget_remain_visible(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot("running", engine_source="pi", model_response_timeout_seconds=90))
        assert "当前引擎：Pi" in panel.engine_label.text()
        assert "响应空闲时限：90 秒" in panel.engine_label.text()
        assert "任务和工具另有预算" in panel.engine_label.text()
        assert panel.engine_fallback_label.isHidden()
        service.updated.emit(_snapshot(
            "running", engine_source="pydantic", model_response_timeout_seconds=90,
            engine_fallback_reason="Pi 等待有效响应超时，已转入内置引擎。",
        ))
        assert "内置持续引擎（PydanticAI）" in panel.engine_label.text()
        assert not panel.engine_fallback_label.isHidden()
        assert "等待有效响应超时" in panel.engine_fallback_label.text()
        service.activity.emit("agent_reasoning", {"message": "正在检查安装孔", "raw_reasoning": "PRIVATE"})
        service.activity.emit("pi_event", {"message": "Pi 会话处理中", "event": "message_update"})
        assert "内置持续引擎" in panel.engine_label.text()
        assert "等待有效响应超时" in panel.engine_fallback_label.text()
        assert panel.phase_label.text() == "正在检查安装孔"
        assert "Pi 会话处理中" not in panel.log.toPlainText()
        assert "PRIVATE" not in panel.log.toPlainText()
        service.updated.emit(_snapshot("review", engine_source="pydantic"))
        panel.new_draft()
        assert "尚未开始" in panel.engine_label.text()
        assert panel.engine_fallback_label.isHidden()
    finally:
        panel.close()


def test_review_keeps_concrete_blocker_and_continue_is_not_new_geometry(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        blocker = "当前草图有相交轮廓；需要修正圆半径后再验证。"
        service.updated.emit(_snapshot("review", message=blocker))
        service.activity.emit("agent_reasoning", {"message": "正在结合当前模型决定下一阶段"})
        service.activity.emit("pi_event", {"message": "Pi 会话处理中", "event": "message_update"})
        assert blocker in panel.phase_label.text()
        assert blocker in panel.review_message.text()
        assert not panel.feedback_requirements.isChecked()
        panel.prompt.setPlainText("继续")
        panel.send_button.click()
        assert service.calls == [("resume_and_run", "继续")]
        assert not panel.feedback_requirements.isChecked()
    finally:
        panel.close()


def test_historical_control_requirement_is_traceable_but_not_geometry_evidence(qapp):
    service = FakeContinuous()
    panel = TaskPanel(service)
    try:
        service.updated.emit(_snapshot(
            "completed",
            requirements=[
                {"id": "r1", "description": "主体宽度", "source_kind": "requirement", "goal_ids": ["width"]},
                {"id": "r2", "description": "继续", "source_text": "继续", "source_kind": "control",
                 "parent_id": "r1", "goal_ids": ["legacy-unverified"]},
            ],
            goals=[{"id": "width", "description": "主体宽度", "expected": 60}],
            verification={"passed": True, "goals": [
                {"id": "width", "description": "主体宽度", "passed": True, "observable": True,
                 "expected": 60, "actual": 60},
            ]},
        ))
        assert panel.remaining_label.text() == "需求 1 项 · 0 项尚缺完成证据"
        text = panel.checklist.toPlainText()
        assert "✓ 主体宽度" in text
        assert "控制消息（不作为几何验收）\n• r2：继续" in text
        assert "□ 继续" not in text
        assert "缺少对应验收结果" not in text
    finally:
        panel.close()
