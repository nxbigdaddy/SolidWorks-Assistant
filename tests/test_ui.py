from PySide6.QtWidgets import QLabel
from PySide6.QtTest import QTest

from sw_assistant.domain import Plan, ToolCall
from sw_assistant.infrastructure import AppPaths, AppSettings
from sw_assistant.runtime import ApplicationController
from sw_assistant.ui import MainWindow
from sw_assistant.ui.settings_dialog import SettingsDialog


def _paths(tmp_path):
    return AppPaths(
        data=tmp_path,
        logs=tmp_path / "logs",
        settings=tmp_path / "settings.json",
        library_db=tmp_path / "library.db",
        history_db=tmp_path / "history.db",
    )


def test_main_window_has_integrated_pages_and_custom_titlebar(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        assert window.windowFlags() & window.windowFlags().FramelessWindowHint
        assert [button.text() for button in window.nav_buttons] == [
            "智能建模",
            "本地模型库",
            "自动化中心",
            "历史与诊断",
        ]
        labels = [label.text() for label in window.title_bar.findChildren(QLabel)]
        assert "SolidWorks 智能建模助手" in labels
        assert "5.3.0 · 交互建模 · 检查点恢复" in labels
        assert window.pages.count() == 4
        assert "持续任务：模型服务未配置" in window.mode_label.text()
    finally:
        window.close()


def test_model_mode_hint_follows_continuous_legacy_and_settings(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window._show_legacy_model_page()
        assert window.mode_label.text() == "兼容计划：本地规则规划器"
        window._navigate(0)
        assert "模型服务未配置" in window.mode_label.text()
        controller.settings.model.api_base = "http://127.0.0.1:1/v1"
        controller.settings.model.model = "test-model"
        window._update_mode()
        assert "test-model" in window.mode_label.text()
        assert "自动（优先 Pi）" in window.mode_label.text()
        controller.settings.agent_backend = "pydantic"
        window._update_mode()
        assert "内置持续会话" in window.mode_label.text()
        controller.settings.model.credential_unreadable = True
        window._update_mode()
        assert "模型凭据需要更新" in window.mode_label.text()
    finally:
        window.close()


def test_settings_dialog_edits_bounded_agent_preferences(qapp):
    dialog = SettingsDialog(AppSettings())
    try:
        assert dialog.agent_enabled.isChecked()
        dialog.agent_enabled.setChecked(False)
        dialog.agent_max_model_requests.setValue(7)
        dialog.agent_backend.setCurrentIndex(dialog.agent_backend.findData("pi"))
        dialog.pi_max_repair_rounds.setValue(10)

        settings = dialog.value()

        assert settings.agent_enabled is False
        assert settings.agent_max_model_requests == 7
        assert settings.agent_backend == "pi"
        assert settings.pi_max_repair_rounds == 10
    finally:
        dialog.close()


def test_validated_plan_has_local_geometry_preview(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    plan = Plan(
        "圆柱",
        [
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "front"}),
            ToolCall("sketch.circle", {"radius_mm": 20}),
            ToolCall("feature.extrude", {"depth_mm": 30, "operation": "boss"}),
        ],
        assumptions=["未指定材料，质量不作为验收条件。"],
    )
    try:
        window.plan = plan
        window._render_plan_table()
        assert window.plan_preview.plane == "front"
        assert window.plan_preview.primitives == [
            {"type": "circle", "x": 0.0, "y": 0.0, "radius": 20.0}
        ]
        assert "拉伸 30 mm" in window.plan_preview.features[0]
        assert window.plan_assumptions.isHidden() is False
        assert "执行前可审查" in window.plan_assumptions.text()
        assert "未指定材料" in window.plan_assumptions.text()
    finally:
        window.close()


def test_plan_click_shows_immediate_progress_and_stop(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    submitted = []
    controller.plan_async = submitted.append
    window = MainWindow(controller)
    try:
        window.prompt.setPlainText("画半径 10 mm 的圆")
        window.generate_button.click()
        assert submitted == ["画半径 10 mm 的圆"]
        assert window.progress_card.isHidden() is False
        assert window.plan_progress.maximum() == 0
        assert window.stop_button.isEnabled()
        assert window.generate_button.isEnabled() is False
    finally:
        window.close()


def test_planning_failure_is_inline_and_preserves_prompt(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window.prompt.setPlainText("task")
        window._on_event("planning_failed", {"message": "API Key 无法解密"})
        assert "API Key" in window.progress_detail.text()
        assert window.prompt.toPlainText() == "task"
        assert window.generate_button.isEnabled()
    finally:
        window.close()


def test_planning_clarification_is_not_shown_as_generic_failure(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window.prompt.setPlainText("画圆并拉伸")
        window._on_event(
            "planning_clarification",
            {
                "questions": [
                    "请提供明确的圆半径或直径（毫米）。",
                    "请提供明确的拉伸或切除深度（毫米）。",
                ]
            },
        )
        assert window.progress_title.text() == "需要补充建模条件"
        assert "半径或直径" in window.progress_detail.text()
        assert window.prompt.toPlainText() == "画圆并拉伸"
        assert window.generate_button.text() == "补充条件后重新生成"
        assert window.prompt.isEnabled()
        assert window.prompt.isReadOnly() is False
        assert window.clarification_input.isHidden() is False
        assert window.clarification_input.isEnabled()
        assert window.clarification_input.isReadOnly() is False
        assert window.clarification_submit_button.isHidden() is False
    finally:
        window.close()


def test_clarification_input_accepts_typing_and_resubmits_combined_prompt(
    qapp, tmp_path
):
    controller = ApplicationController(_paths(tmp_path))
    submitted = []
    controller.plan_async = submitted.append
    window = MainWindow(controller)
    try:
        window.show()
        window.prompt.setPlainText("画一个 iphone 手机")
        window._on_event(
            "planning_clarification",
            {
                "questions": [
                    "请提供产品整体长×宽×厚。",
                    "请确认是否创建无圆角简化主体。",
                ]
            },
        )
        qapp.processEvents()
        window.clarification_input.setFocus()
        QTest.keyClicks(
            window.clarification_input,
            "161 x 78 x 8 mm; simplified body",
        )
        assert window.clarification_input.toPlainText() == (
            "161 x 78 x 8 mm; simplified body"
        )

        window.clarification_submit_button.click()
        assert submitted == [
            "画一个 iphone 手机\n补充条件：161 x 78 x 8 mm; simplified body"
        ]
        assert window.prompt.toPlainText() == submitted[0]
        assert window.clarification_input.isHidden()
        assert window.generate_button.isEnabled() is False
    finally:
        window.close()


def test_blank_clarification_stays_editable_and_does_not_resubmit(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    submitted = []
    controller.plan_async = submitted.append
    window = MainWindow(controller)
    try:
        window.prompt.setPlainText("画一个手机")
        window._on_event("planning_clarification", {"questions": ["请补充尺寸。"]})
        window.clarification_submit_button.click()
        assert submitted == []
        assert window.clarification_input.isVisible() or not window.isVisible()
        assert window.clarification_input.isEnabled()
        assert window.runtime_status.text() == "请先填写补充建模条件。"
    finally:
        window.close()


def test_model_repair_temporarily_disables_every_recovery_action(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window._on_event("repair_started", {"request_id": 1})
        assert all(button.isEnabled() is False for button in window.recovery_buttons)
        window._on_event("repair_failed", {"message": "repair failed"})
        assert all(button.isEnabled() for button in window.recovery_buttons)
    finally:
        window.close()


def test_pi_repair_and_fallback_status_are_visible(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window._on_event(
            "repair_started",
            {"request_id": 1, "automatic": True, "agent_backend": "pi"},
        )
        assert "Pi Agent" in window.recovery_message.text()
        window._on_event(
            "pi_agent_fallback",
            {"message": "Pi 不可用，已回退内置监督器。"},
        )
        assert "已回退" in window.recovery_message.text()
    finally:
        window.close()


def test_library_task_disables_conflicting_actions(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window._on_event("library_started", {"kind": "scan", "task_id": "one"})
        assert window.library_scan_button.isEnabled() is False
        assert window.deep_button.isEnabled() is False
        assert window.copy_part_button.isEnabled() is False
        assert window.product_family_button.isEnabled() is False
        assert window.export_button.isEnabled() is False
        assert window.library_stop.isEnabled()
        window._on_event(
            "library_finished",
            {
                "total": 0,
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "cancelled": False,
            },
        )
        assert window.library_scan_button.isEnabled()
        assert window.deep_button.isEnabled()
        assert window.copy_part_button.isEnabled()
        assert window.product_family_button.isEnabled()
        assert window.export_button.isEnabled()
        assert window.library_stop.isEnabled() is False
    finally:
        window.close()


def test_cancelled_derivation_restores_library_actions(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window._on_event("derivation_started", {"strategy": "byte_copy"})
        window._on_event("derivation_cancelled", {"message": "任务已停止。"})
        assert window.library_status.text() == "派生已取消"
        assert window.library_stop.isEnabled() is False
        assert window.copy_part_button.isEnabled()
    finally:
        window.close()


def test_analysis_result_is_rendered_in_library_detail(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        window._on_event(
            "analysis_started", {"kind": "similar", "task_id": "analysis"}
        )
        assert window.library_progress.maximum() == 0
        assert window.library_stop.isEnabled()
        window._on_event(
            "analysis_succeeded",
            {"kind": "similar", "result": [{"name": "Part2", "score": 0.9}]},
        )
        assert "Part2" in window.library_detail.toPlainText()
        assert "0.9" in window.library_detail.toPlainText()
        assert window.library_stop.isEnabled() is False
    finally:
        window.close()


def test_named_target_reference_list_is_visible_and_restorable(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    source = tmp_path / "Part1.SLDPRT"
    source.write_bytes(b"part")
    reference = controller.library_repository.save_selection_reference(
        document_path=str(source),
        document_title="Part1.SLDPRT",
        alias="安装面",
        persistent_ref="AQID",
        selection_type=2,
        object_name="Face1",
    )
    restored = []
    controller.restore_selection_reference = restored.append
    window = MainWindow(controller)
    try:
        controller.refresh_selection_references()
        assert "安装面" in window.target_references.currentText()
        window.restore_target_button.click()
        assert restored == [reference.id]
    finally:
        window.close()


def test_document_manager_button_tracks_license_configuration(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    window = MainWindow(controller)
    try:
        assert window.metadata_button.isEnabled() is False
        controller.settings.document_manager_key = "licensed"
        controller.save_settings = lambda _settings: None
        controller.document_manager.license_key = "licensed"
        window._on_event("settings_saved", {})
        assert window.metadata_button.isEnabled()
    finally:
        window.close()


def test_execution_click_disables_duplicate_submission_immediately(qapp, tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    submitted = []
    controller.execute_next = lambda: submitted.append("next")
    window = MainWindow(controller)
    try:
        window._execution_buttons(True)
        window.next_button.click()
        assert submitted == ["next"]
        assert window.next_button.isEnabled() is False
        assert window.run_button.isEnabled() is False
        assert window.stop_button.isEnabled()
    finally:
        window.close()
