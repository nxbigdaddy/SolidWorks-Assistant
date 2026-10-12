from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


_STATUS = {
    "idle": "等待准备",
    "ready": "等待执行",
    "running": "进行中",
    "review": "需要人工检查",
    "completed": "任务完成",
    "stopped": "已停止",
    "failed": "任务遇到问题",
    "interrupted": "上次运行已中断",
}
_PHASE = {
    "prepare": "准备任务",
    "preparing": "准备任务",
    "planning": "分析目标",
    "observe": "读取当前状态",
    "observing": "读取当前状态",
    "decide": "决定下一阶段",
    "deciding": "决定下一阶段",
    "execute": "执行阶段动作",
    "executing": "执行阶段动作",
    "verify": "验证结果",
    "verifying": "验证结果",
    "review": "等待人工检查",
    "completed": "完成验收",
    "stopped": "停止任务",
    "ready": "准备就绪",
    "checkpoint_restore": "恢复检查点副本",
}


class TaskPanel(QWidget):
    """A view over the continuous service; CAD decisions remain outside the UI."""

    def __init__(
        self,
        service: Any,
        parent: QWidget | None = None,
        *,
        stop_all: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.stop_all = stop_all or self.service.stop
        self.snapshot: dict[str, Any] = {}
        self._busy = False
        self._restoring = False
        self._sending_note = False
        self._rejected = False
        self._local_notes: list[str] = []
        self._public_activity: list[str] = []
        self.setStyleSheet(
            "QPushButton#primaryButton:disabled { background: #111b29; color: #6c7e95; "
            "border-color: #223044; }"
            "QLabel#engineFallback { color: #efc58e; }"
        )
        self._build()
        self.service.updated.connect(self.render_snapshot)
        self.service.activity.connect(self.on_activity)
        self.refresh_recent()
        self._update_buttons()

    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("class", "card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        return card, layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 12, 18)
        layout.setSpacing(10)
        title = QLabel("智能建模 · 持续任务")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)
        subtitle = QLabel("发送一个任务，Agent 会调查、执行并检查结果；运行中可以随时补充意见。")
        subtitle.setWordWrap(True)
        subtitle.setProperty("class", "muted")
        layout.addWidget(subtitle)

        input_card, inputs = self._card()
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "例如：画一个直径 40 mm、高 30 mm 的圆柱，在顶面切一个直径 10 mm 的通孔。"
        )
        self.prompt.setMaximumHeight(78)
        self.prompt.textChanged.connect(self._update_buttons)
        inputs.addWidget(self.prompt)
        task_actions = QHBoxLayout()
        self.send_button = QPushButton("发送并开始")
        self.send_button.setObjectName("primaryButton")
        self.send_button.clicked.connect(self._send)
        task_actions.addWidget(self.send_button)
        self.advanced_button = QPushButton("高级操作")
        self.advanced_button.setCheckable(True)
        task_actions.addWidget(self.advanced_button)
        task_actions.addStretch(1)
        self.stop_button = QPushButton("停止所有")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setToolTip("停止当前任务，以及模型库、固定计划和批处理中的动作。")
        self.stop_button.clicked.connect(self._stop)
        task_actions.addWidget(self.stop_button)
        inputs.addLayout(task_actions)
        self.input_hint = QLabel("发送后自动开始调查和建模；可在高级操作中选择仅准备。")
        self.input_hint.setWordWrap(True)
        self.input_hint.setProperty("class", "muted")
        inputs.addWidget(self.input_hint)

        self.advanced_card = QWidget()
        advanced = QVBoxLayout(self.advanced_card)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced_actions = QHBoxLayout()
        self.prepare_button = QPushButton("仅准备")
        self.prepare_button.setToolTip("整理目标、假设和首个阶段；准备期间不会修改 SolidWorks。")
        self.prepare_button.clicked.connect(self._prepare)
        self.run_button = QPushButton("继续执行")
        self.run_button.clicked.connect(lambda: self._invoke("run", "正在持续执行任务…"))
        self.step_button = QPushButton("执行一个阶段")
        self.step_button.clicked.connect(lambda: self._invoke("step", "正在执行一个阶段…"))
        self.new_task_button = QPushButton("新任务")
        self.new_task_button.clicked.connect(self.new_draft)
        for button in (self.prepare_button, self.run_button, self.step_button, self.new_task_button):
            advanced_actions.addWidget(button)
        advanced_actions.addStretch(1)
        advanced.addLayout(advanced_actions)
        history_actions = QHBoxLayout()
        self.recent_tasks = QComboBox()
        self.recent_tasks.setMinimumWidth(160)
        self.recent_tasks.setMaximumWidth(280)
        self.recent_tasks.setToolTip("选择历史任务后点击恢复；不会自动执行。")
        self.recent_tasks.currentIndexChanged.connect(self._update_buttons)
        history_actions.addWidget(self.recent_tasks, 1)
        self.restore_button = QPushButton("恢复任务")
        self.restore_button.clicked.connect(self._restore)
        history_actions.addWidget(self.restore_button)
        history_actions.addStretch(1)
        advanced.addLayout(history_actions)
        checkpoint_actions = QHBoxLayout()
        self.checkpoints = QComboBox()
        self.checkpoints.setMinimumWidth(160)
        self.checkpoints.setToolTip("仅恢复当前任务的已验证几何前缀；当前要求不会回滚，恢复后不会自动执行。")
        self.checkpoints.currentIndexChanged.connect(self._update_buttons)
        checkpoint_actions.addWidget(self.checkpoints, 1)
        self.checkpoint_refresh_button = QPushButton("检查点列表")
        self.checkpoint_refresh_button.clicked.connect(
            lambda: self._invoke("refresh_checkpoints", "正在校验本任务的磁盘检查点…"))
        checkpoint_actions.addWidget(self.checkpoint_refresh_button)
        self.checkpoint_restore_button = QPushButton("恢复检查点副本")
        self.checkpoint_restore_button.clicked.connect(self._restore_checkpoint)
        checkpoint_actions.addWidget(self.checkpoint_restore_button)
        advanced.addLayout(checkpoint_actions)
        self.checkpoint_hint = QLabel("先读取检查点列表，再选择版本恢复；不覆盖、不关闭现有模型，也不自动继续。")
        self.checkpoint_hint.setWordWrap(True)
        self.checkpoint_hint.setProperty("class", "muted")
        advanced.addWidget(self.checkpoint_hint)
        inputs.addWidget(self.advanced_card)
        self.advanced_card.hide()
        self.advanced_button.toggled.connect(self.advanced_card.setVisible)

        status_card, status_layout = self._card()
        status_row = QHBoxLayout()
        self.status_label = QLabel("等待新任务")
        self.status_label.setProperty("class", "sectionTitle")
        self.rounds_label = QLabel("第 0 轮")
        self.rounds_label.setProperty("class", "muted")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.rounds_label)
        status_layout.addLayout(status_row)
        self.engine_label = QLabel("当前引擎：尚未开始")
        self.engine_label.setTextFormat(Qt.TextFormat.PlainText)
        self.engine_label.setWordWrap(True)
        self.engine_label.setProperty("class", "muted")
        self.engine_label.setToolTip("显示实际采用的推理引擎。模型响应空闲时限不等于整个任务时限；任务和工具操作另有预算。")
        status_layout.addWidget(self.engine_label)
        self.engine_fallback_label = QLabel()
        self.engine_fallback_label.setObjectName("engineFallback")
        self.engine_fallback_label.setTextFormat(Qt.TextFormat.PlainText)
        self.engine_fallback_label.setWordWrap(True)
        self.engine_fallback_label.hide()
        status_layout.addWidget(self.engine_fallback_label)
        self.phase_label = QLabel("等待任务。发送后自动推进，不需要逐阶段确认。")
        self.phase_label.setTextFormat(Qt.TextFormat.PlainText)
        self.phase_label.setWordWrap(True)
        status_layout.addWidget(self.phase_label)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(9)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        status_layout.addWidget(self.progress)
        self.remaining_label = QLabel("需求清单将在任务分析后显示。")
        self.remaining_label.setWordWrap(True)
        self.remaining_label.setProperty("class", "muted")
        status_layout.addWidget(self.remaining_label)
        layout.addWidget(status_card)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #25364d; background: #101a28; }"
            "QTabBar::tab { background: #101a28; color: #b8c9de; padding: 8px 14px; }"
            "QTabBar::tab:selected { background: #17365d; color: #eaf3ff; }"
        )
        self.steps = QTableWidget(0, 3)
        self.steps.setHorizontalHeaderLabels(["状态", "阶段动作", "参数 / 结果"])
        self.steps.verticalHeader().hide()
        self.steps.setAlternatingRowColors(True)
        self.steps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.steps.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.steps.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.steps.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("任务对话将显示你的需求、补充意见、Agent 调查进展和工具结果。")
        self.log.setMaximumBlockCount(500)
        self.tabs.addTab(self.log, "任务对话")
        self._steps_tab = self.tabs.addTab(self.steps, "阶段动作")
        self.checklist = QPlainTextEdit()
        self.checklist.setReadOnly(True)
        self.checklist.setPlaceholderText("准备任务后显示目标、假设和验收结果。")
        self.tabs.addTab(self.checklist, "需求与证据")
        layout.addWidget(self.tabs, 1)

        self.review_card, review_layout = self._card()
        self.review_card.setObjectName("recoveryCard")
        self.review_message = QLabel()
        self.review_message.setTextFormat(Qt.TextFormat.PlainText)
        self.review_message.setWordWrap(True)
        review_layout.addWidget(self.review_message)
        self.review_requests = QLabel()
        self.review_requests.setTextFormat(Qt.TextFormat.PlainText)
        self.review_requests.setWordWrap(True)
        review_layout.addWidget(self.review_requests)
        self.investigation_details = QPlainTextEdit()
        self.investigation_details.setReadOnly(True)
        self.investigation_details.setMaximumHeight(100)
        review_layout.addWidget(self.investigation_details)
        self.feedback_requirements = QCheckBox("这条反馈包含新的尺寸或结构要求")
        self.feedback_requirements.setToolTip("勾选后将新增条件记录为需求，并重新规划；仅确认现场操作时无需勾选。")
        review_layout.addWidget(self.feedback_requirements)
        # Keep the existing test/API names while presenting one shared composer.
        self.feedback = self.prompt
        self.resume_button = self.send_button
        self.review_card.hide()
        layout.addWidget(self.review_card)
        layout.addWidget(input_card)

    def new_draft(self) -> None:
        if self._busy:
            self.phase_label.setText("当前任务进行中，请先停止，再准备新任务。")
            return
        self.snapshot = {}
        self._render_checkpoints({})
        self._local_notes.clear()
        self._public_activity.clear()
        self.prompt.clear()
        self.steps.setRowCount(0)
        self.log.clear()
        self.checklist.clear()
        self.feedback.clear()
        self.feedback_requirements.setChecked(False)
        self.review_card.hide()
        self.status_label.setText("等待新任务")
        self.rounds_label.setText("第 0 轮")
        self.phase_label.setText("输入新的建模需求，点击发送并开始。")
        self.remaining_label.setText("需求清单将在任务分析后显示。")
        self._render_engine({})
        self._update_buttons()
        self.prompt.setFocus()

    def _send(self) -> None:
        status = str(self.snapshot.get("status") or "")
        has_task = bool(self.snapshot.get("id"))
        if has_task and status in {"review", "stopped", "failed", "interrupted"}:
            self._resume()
            return
        note = self.prompt.toPlainText().strip()
        if has_task and status in {"ready", "running"}:
            if not note and status == "ready" and not self._busy:
                self._invoke("run", "正在继续任务，将先核对当前状态…")
            elif note:
                self._steer(note)
            return
        if not note:
            self.phase_label.setText("请先描述要完成的任务。")
            self.prompt.setFocus()
            return
        self._invoke("start", "任务已提交，正在调查并自动推进…", note)

    def _steer(self, note: str) -> None:
        if self._sending_note:
            return
        self._sending_note = True
        self._rejected = False
        self._update_buttons()
        try:
            result = self.service.steer(note)
            if result is False or self._rejected:
                return
            self._local_notes.append(note)
            self.prompt.clear()
            self._render_conversation(self.snapshot)
            self.phase_label.setText("补充意见已提交，将在下一个安全边界处理。")
        except Exception as exc:
            self.phase_label.setText("补充意见未提交：" + str(exc))
        finally:
            self._sending_note = False
            self._update_buttons()

    def _prepare(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            self.phase_label.setText("请先描述建模目标。")
            self.prompt.setFocus()
            return
        self._invoke("prepare", "正在整理目标与首个阶段，尚未修改模型…", prompt)

    def _resume(self) -> None:
        note = self.feedback.toPlainText().strip()
        if not note:
            self.review_message.setText("请说明检查或操作结果，再提交反馈。")
            self.feedback.setFocus()
            return
        adds_requirements = self.snapshot.get("status") == "review" and self.feedback_requirements.isChecked()
        method = "resume_requirements_and_run" if adds_requirements else "resume_and_run"
        message = "正在记录新增要求、重新规划并继续任务…" if adds_requirements else "正在核对人工反馈并继续任务…"
        if self._invoke(method, message, note):
            self._local_notes.append(note)
            self.prompt.clear()
            self.feedback_requirements.setChecked(False)
            self._render_conversation(self.snapshot)

    def _restore(self) -> None:
        task_id = self.recent_tasks.currentData()
        if task_id:
            self.feedback_requirements.setChecked(False)
            self._restoring = True
            self._invoke("restore", "正在恢复任务记录，恢复后不会自动执行…", str(task_id))

    def _restore_checkpoint(self) -> None:
        selected = self.checkpoints.currentData()
        if isinstance(selected, dict) and selected.get("host_prefix_available") is True:
            self._invoke("restore_checkpoint", "正在恢复独立检查点副本，完成后等待明确继续…",
                         str(selected["checkpoint_id"]))

    def _render_checkpoints(self, snapshot: Mapping[str, Any]) -> None:
        selected = self.checkpoints.currentData()
        selected_id = selected.get("checkpoint_id") if isinstance(selected, dict) else None
        self.checkpoints.blockSignals(True)
        self.checkpoints.clear()
        self.checkpoints.addItem("选择本任务检查点…", None)
        catalog = snapshot.get("checkpoint_catalog") or []
        for item in catalog:
            if not isinstance(item, dict) or item.get("integrity") != "verified":
                continue
            checkpoint_id = item.get("checkpoint_id")
            if not isinstance(checkpoint_id, str):
                continue
            suffix = f"{item.get('completed_count', 0)} 个成功动作" if item.get("host_prefix_available") else "缺少宿主前缀，不能恢复"
            label = f"{str(item.get('created_at') or '')[:19]} · {checkpoint_id[:8]} · {suffix}"
            self.checkpoints.addItem(label, item)
            if checkpoint_id == selected_id:
                self.checkpoints.setCurrentIndex(self.checkpoints.count() - 1)
        self.checkpoints.blockSignals(False)
        count = len(snapshot.get("checkpoint_records") or [])
        issues = len(snapshot.get("checkpoint_issues") or [])
        self.checkpoint_hint.setText(
            (f"已记录 {count} 个成功检查点，磁盘校验发现 {issues} 项问题。" if issues else
             f"已记录 {count} 个成功检查点。")
            + "读取列表后选择恢复；需求不回滚，不自动继续。未知活动文档须先人工处理。"
        )

    def _invoke(self, method: str, message: str, *args: Any) -> bool:
        if self._busy:
            return False
        self._busy = True
        self._rejected = False
        self.phase_label.setText(message)
        self._update_buttons()
        try:
            result = getattr(self.service, method)(*args)
            if result is False or self._rejected:
                self._busy = bool(self.snapshot.get("busy")) or self.snapshot.get("status") == "running"
                self._update_buttons()
                return False
            return True
        except Exception as exc:
            self._busy = False
            self._restoring = False
            self.phase_label.setText(str(exc))
            self.log.appendPlainText("操作未完成：" + str(exc))
            self._update_buttons()
            return False

    def _stop(self) -> None:
        self.phase_label.setText("正在请求停止所有动作…")
        try:
            self.stop_all()
        except Exception as exc:
            self.phase_label.setText("停止请求遇到问题：" + str(exc))

    def refresh_recent(self) -> None:
        current = self.recent_tasks.currentData()
        self.recent_tasks.blockSignals(True)
        self.recent_tasks.clear()
        self.recent_tasks.addItem("近期任务…", None)
        try:
            records = self.service.recent()
            for record in records:
                if not isinstance(record, Mapping) or not record.get("id"):
                    continue
                label = str(record.get("prompt") or record["id"]).replace("\n", " ")
                status = _STATUS.get(str(record.get("status")), "已保存")
                self.recent_tasks.addItem(f"{status} · {label[:42]}", str(record["id"]))
            index = self.recent_tasks.findData(current)
            self.recent_tasks.setCurrentIndex(max(0, index))
        except Exception as exc:
            self.log.appendPlainText("近期任务读取失败：" + str(exc))
        finally:
            self.recent_tasks.blockSignals(False)
        self._update_buttons()

    @Slot(object)
    def render_snapshot(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        previous_id = self.snapshot.get("id")
        self.snapshot = dict(value)
        status = str(value.get("status") or "ready")
        phase = str(value.get("phase") or "")
        self._busy = bool(value.get("busy")) or status == "running"
        self.status_label.setText(_STATUS.get(status, status))
        self._render_engine(value)
        self.rounds_label.setText(f"第 {value.get('rounds', 0)} 轮")
        message = str(value.get("message") or "")
        phase_text = _PHASE.get(phase, phase)
        progress_allowed = status == "running" or (status == "ready" and bool(value.get("busy")))
        progress_message = str(value.get("agent_progress_message") or "") if progress_allowed else ""
        self.phase_label.setText(" · ".join(part for part in (phase_text, progress_message or message) if part))
        if value.get("id") != previous_id or self._restoring:
            self.feedback_requirements.setChecked(False)
            if value.get("id"):
                self.prompt.clear()
                self._local_notes.clear()
                self._public_activity.clear()
        self._restoring = False
        self._render_steps(value)
        self._render_checkpoints(value)
        self._render_checklist(value)
        self._render_conversation(value)
        needs_review = bool(value.get("id")) and status in {
            "review", "interrupted", "failed", "stopped"
        }
        self.review_card.setVisible(needs_review)
        if needs_review:
            questions = value.get("questions") or []
            self.review_message.setText(
                "当前阻塞：" + (message or "需要核对当前模型或补充条件。")
            )
            history = value.get("diagnostic_history") or []
            investigations = [self._diagnostic_text(item) for item in history[-4:]]
            tried = "\n".join("• " + text for text in investigations if text)
            requests = "\n".join("• " + str(question) for question in questions)
            if not requests:
                requests = "• 请说明已完成的检查、现场操作或需要补充的条件。"
            self.review_requests.setText("需要你提供\n" + requests)
            self.investigation_details.setPlainText(
                "已调查 / 已尝试\n" + (tried or "尚无可用诊断记录。")
            )
        self.progress.setRange(0, 0 if self._busy else 1)
        self.progress.setValue(1 if status == "completed" else 0)
        self._update_buttons()
        if not self._busy:
            self.refresh_recent()

    def _render_engine(self, snapshot: Mapping[str, Any]) -> None:
        source = snapshot.get("engine_source")
        label = {"pi": "Pi", "pydantic": "内置持续引擎（PydanticAI）"}.get(
            str(source), "尚未确认" if snapshot.get("id") else "尚未开始"
        )
        text = "当前引擎：" + label
        timeout = snapshot.get("model_response_timeout_seconds")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            text += f" · 响应空闲时限：{timeout:g} 秒（任务和工具另有预算）"
        self.engine_label.setText(text)
        reason = str(snapshot.get("engine_fallback_reason") or "").strip()
        compact = " ".join(reason.split())
        self.engine_fallback_label.setText("引擎切换原因：" + compact[:220] + ("…" if len(compact) > 220 else ""))
        self.engine_fallback_label.setToolTip(reason)
        self.engine_fallback_label.setVisible(bool(reason))

    def _render_steps(self, snapshot: Mapping[str, Any]) -> None:
        completed = snapshot.get("completed_steps") or []
        pending = snapshot.get("pending_steps") or []
        rows = [("已验证", item) for item in completed if isinstance(item, (dict, str))]
        rows.extend(("待执行", item) for item in pending if isinstance(item, (dict, str)))
        self.steps.setRowCount(len(rows))
        for index, (status, item) in enumerate(rows):
            if isinstance(item, Mapping):
                raw_call = item.get("call")
                call = raw_call if isinstance(raw_call, Mapping) else item
                title = str(call.get("description") or call.get("tool") or call.get("name") or "动作")
                detail = item.get("message") or call.get("arguments") or ""
                if not isinstance(detail, str):
                    detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
            else:
                title, detail = item, ""
            for column, text in enumerate((status, title, detail)):
                cell = QTableWidgetItem(str(text))
                cell.setToolTip(str(text))
                self.steps.setItem(index, column, cell)
        self.tabs.setTabText(self._steps_tab, f"动作 · {len(completed)} 已完成 / {len(pending)} 待执行")

    @staticmethod
    def _readable(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            check_labels = {
                "task_created_document": "任务已创建目标文档",
                "requirements_have_geometry_evidence": "各项要求具有本任务的几何证据",
                "no_active_sketch": "草图已退出编辑",
                "no_feature_errors": "特征无重建错误",
                "part_has_body": "零件包含实体",
                "connected_document": "目标文档已连接",
            }
            title = str(
                value.get("description") or value.get("label") or value.get("name")
                or value.get("goal") or value.get("message")
                or check_labels.get(str(value.get("id")), value.get("id")) or ""
            )
            status = value.get("status")
            if value.get("observable") is False:
                status = "无法可靠读取，待核对"
            elif "passed" in value:
                status = "通过" if value["passed"] else "未通过"
            elif "ok" in value:
                status = "通过" if value["ok"] else "未通过"
            if title:
                text = f"{title}：{status}" if status is not None else title
                if "actual" in value and value.get("actual") is not None:
                    text += f"（目标 {value.get('expected')}，实际 {value['actual']}）"
                elif "expected" in value:
                    text += f"（目标 {value['expected']}）"
                return text
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    def _render_checklist(self, snapshot: Mapping[str, Any]) -> None:
        requirements = self._requirements_with_results(snapshot)
        requirement_lines = [self._requirement_text(item) for item in requirements]
        remaining = sum(not self._requirement_has_evidence(item) for item in requirements)
        if requirements:
            self.remaining_label.setText(
                f"需求 {len(requirements)} 项 · {remaining} 项尚缺完成证据"
            )
        else:
            self.remaining_label.setText("需求覆盖清单尚未形成；普通实体检查不能代表任务已完成。")
        sections = ["需求与子目标\n" + ("\n".join(requirement_lines) or "尚无需求覆盖记录")]
        controls = [item for item in snapshot.get("requirements") or []
                    if isinstance(item, Mapping) and item.get("source_kind") == "control"]
        if controls:
            lines = [f"• {item.get('id') or '记录'}：{item.get('source_text') or item.get('description') or '控制消息'}"
                     for item in controls]
            sections.append("控制消息（不作为几何验收）\n" + "\n".join(lines))
        for key, title in (("goals", "任务目标"), ("assumptions", "采用的假设"), ("verification", "验收结果")):
            raw = snapshot.get(key)
            if isinstance(raw, Mapping):
                if key == "verification" and isinstance(raw.get("goals"), list):
                    values = [
                        {"description": "最终验收", "passed": bool(raw.get("passed"))},
                        *raw["goals"],
                        *raw.get("implicit", []),
                    ]
                elif isinstance(raw.get("checks"), list):
                    values = raw["checks"]
                    summary = raw.get("message") or raw.get("summary")
                    if summary:
                        values = [summary, *values]
                else:
                    values = [raw]
            elif isinstance(raw, list):
                values = raw
            else:
                values = [raw] if raw else []
            lines = ["• " + self._readable(item) for item in values]
            sections.append(title + "\n" + ("\n".join(lines) or "尚无记录"))
        self.checklist.setPlainText("\n\n".join(sections))

    @staticmethod
    def _requirements_with_results(snapshot: Mapping[str, Any]) -> list[Any]:
        values = [item for item in snapshot.get("requirements") or []
                  if not isinstance(item, Mapping) or item.get("source_kind") != "control"]
        by_id = {str(item["id"]): item for item in values if isinstance(item, Mapping) and item.get("id")}
        verification = snapshot.get("verification")
        results = verification.get("goals", []) if isinstance(verification, Mapping) else []
        proof_by_id = {str(item["id"]): item for item in results if isinstance(item, Mapping) and item.get("id")}
        goals = snapshot.get("goals") or []
        goals_by_id = {str(item["id"]): item for item in goals if isinstance(item, Mapping) and item.get("id")}

        def linked(item: Mapping[str, Any], seen: set[str]) -> set[str]:
            identifier = str(item.get("id") or "")
            if identifier in seen:
                return set()
            seen.add(identifier)
            refs = {str(ref) for ref in item.get("goal_ids", [])}
            for child in by_id.values():
                if child.get("parent_id") == identifier:
                    refs.update(linked(child, seen))
            return refs

        enriched = []
        for item in values:
            if not isinstance(item, Mapping) or "goal_ids" not in item:
                enriched.append(item)
                continue
            refs = linked(item, set())
            passed = bool(refs) and all(
                proof_by_id.get(ref, {}).get("passed") is True
                and proof_by_id.get(ref, {}).get("observable") is True
                for ref in refs
            )
            evidence = [
                proof_by_id.get(ref) or goals_by_id.get(ref)
                or {"description": ref, "message": "缺少对应验收结果"}
                for ref in sorted(refs)
            ]
            enriched.append({
                **item, "passed": passed, "evidence": evidence,
                "status": "verified" if passed else "unverified" if refs else "pending",
            })
        return enriched

    @staticmethod
    def _requirement_has_evidence(item: Any) -> bool:
        if not isinstance(item, Mapping):
            return False
        accepted = item.get("passed") is True or item.get("status") in {
            "verified", "completed", "satisfied", "passed"
        }
        return accepted and bool(item.get("evidence") or item.get("verification"))

    def _requirement_text(self, item: Any) -> str:
        if not isinstance(item, Mapping):
            return "□ " + str(item) + " — 待实现 / 待验证"
        title = str(item.get("description") or item.get("text") or item.get("name") or item.get("id") or "需求")
        if item.get("parent_id"):
            title = "↳ " + title
        status = str(item.get("status") or "pending")
        labels = {
            "pending": "待实现", "planned": "已规划，待实施", "in_progress": "进行中",
            "unverified": "待验证", "blocked": "存在阻塞", "unsupported": "能力待补充",
            "verified": "已验证", "completed": "已完成", "satisfied": "已满足",
            "passed": "已通过", "partial": "部分完成", "failed": "未通过",
        }
        has_evidence = self._requirement_has_evidence(item)
        marker = "✓" if has_evidence else "□"
        evidence = item.get("evidence") or item.get("verification")
        if isinstance(evidence, list):
            proof = "；".join(self._diagnostic_text(value) for value in evidence)
        else:
            proof = self._diagnostic_text(evidence) if evidence else "尚无完成证据"
        return f"{marker} {title}\n  状态：{labels.get(status, status)}；证据：{proof}"

    @staticmethod
    def _diagnostic_text(item: Any) -> str:
        if isinstance(item, str):
            return item
        if not isinstance(item, Mapping):
            return ""
        title = str(item.get("question") or item.get("description") or item.get("tool") or "")
        summary = item.get("summary") or item.get("message") or item.get("outcome")
        result = item.get("result")
        if not summary and isinstance(result, Mapping):
            summary = result.get("summary") or result.get("message")
        if not summary and isinstance(result, str):
            summary = result
        if not summary and item.get("actual") is not None:
            summary = f"期望 {item.get('expected')}，读取 {item['actual']}"
        if not summary and item.get("binding") == "pending":
            summary = "验收对象尚待绑定"
        if not summary and "expected" in item and "actual" not in item:
            summary = f"目标 {item['expected']}，尚无验收读回"
        if not summary and item.get("passed") is not None:
            summary = "检查通过" if item["passed"] else "检查未通过"
        return "：".join(str(value) for value in (title, summary) if value) or "已记录，结果待进一步核对"

    def _render_conversation(self, snapshot: Mapping[str, Any]) -> None:
        original = str(snapshot.get("prompt") or "")
        entries: list[tuple[str, int, str]] = []
        user_notes: set[str] = set()
        for message in snapshot.get("messages") or []:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "assistant")
            if role not in {"user", "assistant", "system", "tool"}:
                continue
            if message.get("type") in {"reasoning", "analysis"}:
                continue
            content = message.get("content") or message.get("text") or message.get("message")
            if not isinstance(content, str) or not content:
                continue
            if role == "user":
                user_notes.add(content)
                if content == original:
                    continue
            label = {"user": "你", "assistant": "Agent", "system": "系统", "tool": "工具"}[role]
            timestamp = str(message.get("time") or message.get("timestamp") or message.get("created_at") or "9999")
            entries.append((timestamp, len(entries), f"{label}\n{content}"))
        for event in snapshot.get("events") or []:
            text = self._event_text(event)
            if not text:
                continue
            stamp = str(event.get("time") or event.get("timestamp") or "9999") if isinstance(event, Mapping) else "9999"
            entries.append((stamp, len(entries), "Agent · 进展\n" + text))
        lines = ["你 · 任务\n" + original] if original else []
        lines.extend(entry[2] for entry in sorted(entries))
        self._local_notes = [note for note in self._local_notes if note not in user_notes]
        lines.extend("你 · 补充（下个安全边界处理）\n" + note for note in self._local_notes)
        recorded = "\n".join(lines)
        lines.extend("Agent · 进展\n" + text for text in self._public_activity if text not in recorded)
        scrollbar = self.log.verticalScrollBar()
        position = scrollbar.value()
        follow = position >= scrollbar.maximum() - 8
        self.log.setPlainText("\n\n".join(lines[-500:]))
        scrollbar.setValue(scrollbar.maximum() if follow else position)

    @staticmethod
    def _event_text(event: Any) -> str:
        if isinstance(event, str):
            return event
        if not isinstance(event, Mapping):
            return ""
        message = event.get("message") or event.get("summary")
        name = event.get("name")
        if not message and name == "agent_decision":
            decision = event.get("decision")
            if isinstance(decision, Mapping):
                message = decision.get("reason")
        if not message and name in {"investigation_completed", "diagnostic_completed"}:
            result = event.get("result")
            if isinstance(result, Mapping):
                message = "调查结果：" + TaskPanel._diagnostic_text(result)
        if not message and name in {"step_started", "step_succeeded", "step_failed"}:
            call = event.get("call")
            if isinstance(call, Mapping):
                label = {"step_started": "开始", "step_succeeded": "已验证", "step_failed": "失败"}[str(name)]
                message = f"{label}：{call.get('description') or call.get('tool') or '当前动作'}"
                result = event.get("result")
                if isinstance(result, Mapping) and result.get("message"):
                    message += "；" + str(result["message"])
        if not message:
            message = {
                "task_preparing": "正在准备任务目标和首个阶段。",
                "agent_thinking": "Agent 正在根据实际反馈判断下一阶段。",
                "observation_started": "正在读取当前模型状态。",
                "observation_completed": "当前模型状态读取完成。",
                "stage_started": "开始执行当前阶段。",
                "stage_completed": "本阶段执行完成，等待下一阶段决策。",
                "task_restored": "任务记录已恢复，尚未执行模型动作。",
                "task_resumed": "人工反馈已保存，继续执行时将先核对现场。",
                "task_stopped": "任务已停止。",
                "task_completed": "任务已通过最终验收。",
            }.get(str(name))
        if not message:
            return ""
        timestamp = str(event.get("time") or event.get("timestamp") or event.get("created_at") or "")
        return f"{timestamp}  {message}".strip()

    @Slot(str, object)
    def on_activity(self, name: str, payload: object) -> None:
        # Display the service's public summary, never arbitrary model payloads.
        if not isinstance(payload, Mapping):
            return
        cad_progress_allowed = (self.snapshot.get("status") == "running"
                                or (self.snapshot.get("status") == "ready" and bool(self.snapshot.get("busy"))))
        if name == "cad_progress" and not cad_progress_allowed:
            return  # A stopped/reviewed task must not display a late CAD phase.
        if name == "task_rejected":
            self._rejected = True
        task_id = payload.get("task_id") or payload.get("id")
        if task_id and self.snapshot.get("id") and task_id != self.snapshot["id"]:
            return
        if name == "pi_event" and self.snapshot.get("engine_source") == "pydantic":
            return
        message = self._event_text({"name": name, **payload})
        if message:
            if not self._public_activity or self._public_activity[-1] != message:
                self._public_activity.append(message)
                self._public_activity = self._public_activity[-60:]
            self._render_conversation(self.snapshot)
            if self.snapshot.get("status") not in {"review", "stopped", "failed", "interrupted", "completed"} or name == "task_rejected":
                self.phase_label.setText(str(message))

    def _update_buttons(self, *_args: Any) -> None:
        if not hasattr(self, "restore_button"):
            return
        status = str(self.snapshot.get("status") or "")
        has_task = bool(self.snapshot.get("id"))
        note = bool(self.prompt.toPlainText().strip())
        ready = status == "ready" and has_task
        review = has_task and status in {"review", "stopped", "failed", "interrupted"}
        can_steer = has_task and status in {"running", "ready"}
        self.feedback_requirements.setVisible(has_task and status == "review")
        self.feedback_requirements.setEnabled(has_task and status == "review" and not self._busy)
        if status != "review":
            self.feedback_requirements.setChecked(False)
        self.prepare_button.setEnabled(not self._busy and not can_steer and not review)
        self.prompt.setReadOnly(self._busy and not can_steer)
        self.run_button.setEnabled(not self._busy and ready and not note)
        self.step_button.setEnabled(not self._busy and ready and not note)
        self.new_task_button.setEnabled(not self._busy)
        hint = "请先发送输入框中的补充意见。" if note and has_task else "继续已保存的任务。"
        self.run_button.setToolTip(hint)
        self.step_button.setToolTip(hint)
        self.recent_tasks.setEnabled(not self._busy)
        self.restore_button.setEnabled(not self._busy and bool(self.recent_tasks.currentData()))
        if hasattr(self, "checkpoint_restore_button"):
            supported = callable(getattr(self.service, "refresh_checkpoints", None))
            self.checkpoint_refresh_button.setEnabled(not self._busy and has_task and supported)
            self.checkpoints.setEnabled(not self._busy and has_task)
            selected = self.checkpoints.currentData()
            available = (isinstance(selected, dict) and selected.get("host_prefix_available") is True
                         and selected.get("integrity") == "verified")
            self.checkpoint_restore_button.setEnabled(not self._busy and has_task and available
                                                      and callable(getattr(self.service, "restore_checkpoint", None)))
        if review:
            self.send_button.setText("反馈并继续")
            self.send_button.setEnabled(not self._busy and not self._sending_note)
            self.input_hint.setText("在这里填写人工检查结果或补充条件；提交后先核对现场，再自动继续。")
            self.prompt.setPlaceholderText("说明已完成的人工检查、操作结果或需要补充的条件。")
        elif can_steer:
            self.send_button.setText("继续执行" if ready and not note and not self._busy else "发送补充")
            self.send_button.setEnabled(not self._sending_note and (note or (ready and not self._busy)))
            self.input_hint.setText("补充意见在下一个安全边界处理，不会中断正在完成的单个操作。")
            self.prompt.setPlaceholderText("随时补充要求或指出问题，Agent 将结合现场继续处理。")
        else:
            self.send_button.setText("发送并开始")
            self.send_button.setEnabled(not self._busy)
            self.input_hint.setText("发送后自动开始调查和建模；可在高级操作中选择仅准备。")
            self.prompt.setPlaceholderText("描述要完成的任务，包括主要形状、尺寸或约束。")
        self.stop_button.setEnabled(True)
        self.progress.setRange(0, 0 if self._busy else 1)
