from __future__ import annotations

import html
import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..advanced import (
    ContextSnapshot,
    DrawingSpec,
    DrawingViewSpec,
    FamilyVariant,
    MateRequest,
    ProductFamilySpec,
)
from ..domain import Plan, ToolCall
from ..library import DocumentRecord, LibraryProgress
from ..runtime import ApplicationController
from .settings_dialog import SettingsDialog
from .plan_preview import PlanPreview
from .task_panel import TaskPanel
from .title_bar import TitleBar


STATUS = {
    "pending": ("等待", QColor("#91a3b8")),
    "running": ("执行中", QColor("#f1c76c")),
    "succeeded": ("成功", QColor("#61d49b")),
    "failed": ("失败", QColor("#ff8092")),
    "skipped": ("跳过", QColor("#7b899a")),
}


class MainWindow(QMainWindow):
    def __init__(self, controller: ApplicationController | None = None) -> None:
        super().__init__()
        self.controller = controller or ApplicationController()
        self.plan: Plan | None = None
        self.step_status: list[str] = []
        self.failed_result: dict | None = None
        self.documents: list[DocumentRecord] = []
        self._planning_started = 0.0
        self._planning_active = False
        self._context_allows_execution = True
        self._execution_requested_enabled = False
        self._execution_preserve_stop = False
        self.latest_recipe_id = ""
        self.setWindowTitle("SolidWorks 智能建模助手")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setMinimumSize(1220, 760)
        self.resize(1540, 900)
        root = QWidget()
        root.setObjectName("appRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)
        outer.addWidget(self._build_body(), 1)
        self.setCentralWidget(root)
        self._build_menus()
        self.controller.activity.connect(self._on_event)
        self.controller.plan_ready.connect(self._on_plan_ready)
        self.controller.documents_ready.connect(self._on_documents)
        self.controller.selection_references_ready.connect(self._on_selection_references)
        self.controller.context_ready.connect(self._on_context)
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._tick_elapsed)
        self._execution_buttons(False)
        self.controller.refresh_documents()
        self.controller.refresh_selection_references()
        self.controller.refresh_jobs()
        if self.controller.settings_store.warning:
            self._append_log("设置", self.controller.settings_store.warning)
            self._show_progress(
                "模型服务凭据需要处理",
                self.controller.settings_store.warning,
                state="failed",
                indeterminate=False,
            )

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.pages = QStackedWidget()
        self.model_pages = QStackedWidget()
        self.task_panel = TaskPanel(self.controller.continuous, stop_all=self.controller.abort_all)
        self.model_pages.addWidget(self.task_panel)
        self.model_pages.addWidget(self._build_model_page())
        self.pages.addWidget(self.model_pages)
        self.pages.addWidget(self._build_library_page())
        self.pages.addWidget(self._build_automation_page())
        self.pages.addWidget(self._build_history_page())
        splitter.addWidget(self.pages)
        splitter.addWidget(self._build_activity_panel())
        splitter.setSizes([1040, 360])
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)
        return body

    def _build_sidebar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("sideBar")
        frame.setFixedWidth(210)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 18, 12, 14)
        label = QLabel("工作区")
        label.setProperty("class", "muted")
        layout.addWidget(label)
        self.nav_buttons: list[QPushButton] = []
        for index, text in enumerate(("智能建模", "本地模型库", "自动化中心", "历史与诊断")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("class", "navButton")
            button.clicked.connect(lambda checked=False, value=index: self._navigate(value))
            layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)
        layout.addStretch(1)
        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        self.mode_label.setProperty("class", "muted")
        self._update_mode()
        layout.addWidget(self.mode_label)
        return frame

    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setProperty("class", "card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        return frame, layout

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 12, 18)
        title = QLabel("智能建模")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "描述目标；Agent 会检查工具、主动披露合理假设，再生成受限计划并逐步验证。"
        )
        subtitle.setProperty("class", "muted")
        layout.addWidget(subtitle)
        input_card, input_layout = self._card()
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "例如：新建零件，在前视基准面绘制半径 20 mm 的圆，盲拉伸 30 mm。"
        )
        self.prompt.setMaximumHeight(130)
        input_layout.addWidget(self.prompt)
        actions = QHBoxLayout()
        self.generate_button = QPushButton("让 Agent 生成并校验计划")
        self.generate_button.setObjectName("primaryButton")
        self.generate_button.clicked.connect(self._generate_plan)
        self.new_button = QPushButton("新任务")
        self.new_button.clicked.connect(self._new_task)
        actions.addWidget(self.generate_button)
        actions.addWidget(self.new_button)
        actions.addStretch(1)
        input_layout.addLayout(actions)
        layout.addWidget(input_card)
        self.progress_card, progress_layout = self._card()
        self.progress_card.setObjectName("progressCard")
        progress_header = QHBoxLayout()
        self.progress_title = QLabel("计划生成进度")
        self.progress_title.setObjectName("progressTitle")
        self.elapsed_label = QLabel("0 秒")
        self.elapsed_label.setObjectName("elapsedLabel")
        progress_header.addWidget(self.progress_title)
        progress_header.addStretch(1)
        progress_header.addWidget(self.elapsed_label)
        progress_layout.addLayout(progress_header)
        self.progress_detail = QLabel("等待开始")
        self.progress_detail.setObjectName("progressDetail")
        self.progress_detail.setWordWrap(True)
        progress_layout.addWidget(self.progress_detail)
        self.clarification_input = QPlainTextEdit()
        self.clarification_input.setObjectName("clarificationInput")
        self.clarification_input.setPlaceholderText(
            "仅在 Agent 判断设计接口或精确规格无法安全假设时，在这里一次性补充条件。"
        )
        self.clarification_input.setMaximumHeight(96)
        self.clarification_input.hide()
        progress_layout.addWidget(self.clarification_input)
        self.clarification_submit_button = QPushButton("提交补充并重新生成")
        self.clarification_submit_button.setObjectName("primaryButton")
        self.clarification_submit_button.clicked.connect(self._submit_clarification)
        self.clarification_submit_button.hide()
        progress_layout.addWidget(
            self.clarification_submit_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        self.plan_progress = QProgressBar()
        self.plan_progress.setTextVisible(False)
        progress_layout.addWidget(self.plan_progress)
        self.progress_card.hide()
        layout.addWidget(self.progress_card)
        plan_card, plan_layout = self._card()
        self.plan_summary = QLabel("尚未生成计划")
        self.plan_summary.setProperty("class", "sectionTitle")
        plan_layout.addWidget(self.plan_summary)
        self.plan_assumptions = QLabel("")
        self.plan_assumptions.setWordWrap(True)
        self.plan_assumptions.setProperty("class", "muted")
        self.plan_assumptions.hide()
        plan_layout.addWidget(self.plan_assumptions)
        self.plan_table = QTableWidget(0, 4)
        self.plan_table.setHorizontalHeaderLabels(["#", "状态", "工具", "参数"])
        self.plan_table.setAlternatingRowColors(True)
        self.plan_table.verticalHeader().hide()
        self.plan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.plan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.plan_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        plan_splitter = QSplitter(Qt.Orientation.Horizontal)
        plan_splitter.addWidget(self.plan_table)
        self.plan_preview = PlanPreview()
        plan_splitter.addWidget(self.plan_preview)
        plan_splitter.setSizes([720, 320])
        plan_splitter.setStretchFactor(0, 1)
        plan_layout.addWidget(plan_splitter, 1)
        execution = QHBoxLayout()
        self.next_button = QPushButton("执行下一步")
        self.next_button.setObjectName("primaryButton")
        self.next_button.clicked.connect(self._execute_next)
        self.run_button = QPushButton("执行全部")
        self.run_button.clicked.connect(self._run_all)
        self.stop_button = QPushButton("停止全部")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self.controller.abort_all)
        execution.addWidget(self.next_button)
        execution.addWidget(self.run_button)
        execution.addStretch(1)
        execution.addWidget(self.stop_button)
        plan_layout.addLayout(execution)
        targets = QHBoxLayout()
        target_label = QLabel("命名目标")
        target_label.setProperty("class", "muted")
        self.target_references = QComboBox()
        self.target_references.setMinimumWidth(260)
        self.target_references.setPlaceholderText("尚未保存目标引用")
        self.target_references.currentIndexChanged.connect(
            lambda _index: self._set_target_reference_busy(False)
        )
        self.capture_target_button = QPushButton("保存当前选择…")
        self.capture_target_button.clicked.connect(self._capture_target_reference)
        self.restore_target_button = QPushButton("恢复选择")
        self.restore_target_button.clicked.connect(self._restore_target_reference)
        self.delete_target_button = QPushButton("删除")
        self.delete_target_button.clicked.connect(self._delete_target_reference)
        targets.addWidget(target_label)
        targets.addWidget(self.target_references, 1)
        targets.addWidget(self.capture_target_button)
        targets.addWidget(self.restore_target_button)
        targets.addWidget(self.delete_target_button)
        plan_layout.addLayout(targets)
        self.recovery_card = QFrame()
        self.recovery_card.setObjectName("recoveryCard")
        recovery = QVBoxLayout(self.recovery_card)
        self.recovery_message = QLabel()
        self.recovery_message.setWordWrap(True)
        recovery.addWidget(self.recovery_message)
        recovery_actions = QHBoxLayout()
        retry = QPushButton("调整环境后重试")
        retry.clicked.connect(self.controller.retry_failed)
        edit = QPushButton("编辑参数并重试")
        edit.clicked.connect(self._edit_failed)
        manual = QPushButton("人工完成后验证")
        manual.clicked.connect(self.controller.verify_manual_completion)
        self.repair_button = QPushButton("让模型修复后续计划")
        self.repair_button.clicked.connect(self.controller.repair_failed_async)
        skip = QPushButton("跳过")
        skip.setObjectName("dangerButton")
        skip.clicked.connect(self._skip_failed)
        self.recovery_buttons = (retry, edit, manual, self.repair_button, skip)
        for button in self.recovery_buttons:
            recovery_actions.addWidget(button)
        recovery.addLayout(recovery_actions)
        self.recovery_card.hide()
        plan_layout.addWidget(self.recovery_card)
        layout.addWidget(plan_card, 1)
        return page

    def _build_library_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 12, 18)
        title = QLabel("本地模型库")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)
        subtitle = QLabel("快速扫描不启动 SolidWorks；深度读取才连接 COM 工作进程。")
        subtitle.setProperty("class", "muted")
        layout.addWidget(subtitle)
        toolbar, toolbar_layout = self._card()
        root_row = QHBoxLayout()
        self.library_root = QLineEdit()
        self.library_root.setPlaceholderText("选择包含 SolidWorks 文件的目录")
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._choose_library_root)
        self.library_scan_button = QPushButton("快速扫描")
        self.library_scan_button.setObjectName("primaryButton")
        self.library_scan_button.clicked.connect(self._scan_library)
        self.deep_button = QPushButton("深度读取全部参数")
        self.deep_button.clicked.connect(self._deep_read)
        self.metadata_button = QPushButton("无界面读取属性")
        self.metadata_button.clicked.connect(self._metadata_read)
        self.metadata_button.setToolTip("需要单独申请的 SolidWorks Document Manager 许可证密钥")
        self.metadata_button.setEnabled(self.controller.document_manager.configured)
        self.library_stop = QPushButton("停止")
        self.library_stop.setObjectName("dangerButton")
        self.library_stop.clicked.connect(self.controller.abort_all)
        self.library_stop.setEnabled(False)
        root_row.addWidget(self.library_root, 1)
        for button in (
            browse,
            self.library_scan_button,
            self.metadata_button,
            self.deep_button,
            self.library_stop,
        ):
            root_row.addWidget(button)
        toolbar_layout.addLayout(root_row)
        self.library_progress = QProgressBar()
        self.library_progress.setValue(0)
        toolbar_layout.addWidget(self.library_progress)
        self.library_status = QLabel("等待任务")
        self.library_status.setProperty("class", "muted")
        toolbar_layout.addWidget(self.library_status)
        layout.addWidget(toolbar)
        search_row = QHBoxLayout()
        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText("按名称或路径搜索")
        self.library_search.returnPressed.connect(self._refresh_library)
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self._refresh_library)
        search_row.addWidget(self.library_search, 1)
        search_row.addWidget(refresh)
        layout.addLayout(search_row)
        derive_row = QHBoxLayout()
        self.copy_part_button = QPushButton("复制选中零件")
        self.copy_part_button.clicked.connect(self._copy_selected_part)
        self.derive_part_button = QPushButton("修改副本参数…")
        self.derive_part_button.clicked.connect(self._derive_selected_part)
        self.product_family_button = QPushButton("生成产品族…")
        self.product_family_button.clicked.connect(self._generate_product_family)
        self.pack_and_go_button = QPushButton("打包装配体…")
        self.pack_and_go_button.clicked.connect(self._pack_and_go_selected_assembly)
        self.export_button = QPushButton("导出副本…")
        self.export_button.clicked.connect(self._export_selected_document)
        derive_row.addWidget(self.copy_part_button)
        derive_row.addWidget(self.derive_part_button)
        derive_row.addWidget(self.product_family_button)
        derive_row.addWidget(self.pack_and_go_button)
        derive_row.addWidget(self.export_button)
        derive_row.addStretch(1)
        self.library_previous = QPushButton("上一页")
        self.library_previous.clicked.connect(self._previous_library_page)
        self.library_page = QLabel("0 / 0")
        self.library_page.setProperty("class", "muted")
        self.library_next = QPushButton("下一页")
        self.library_next.clicked.connect(self._next_library_page)
        derive_row.addWidget(self.library_previous)
        derive_row.addWidget(self.library_page)
        derive_row.addWidget(self.library_next)
        layout.addLayout(derive_row)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.library_table = QTableWidget(0, 6)
        self.library_table.setHorizontalHeaderLabels(
            ["名称", "类型", "参数状态", "参数哈希", "大小", "路径"]
        )
        self.library_table.verticalHeader().hide()
        self.library_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.library_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.library_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.library_table.itemSelectionChanged.connect(self._show_document_detail)
        library_header = self.library_table.horizontalHeader()
        for index in range(5):
            library_header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        library_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.library_table)
        self.library_detail = QTextEdit()
        self.library_detail.setReadOnly(True)
        self.library_detail.setPlaceholderText("选择模型后显示已保存的完整参数 Manifest。")
        splitter.addWidget(self.library_detail)
        splitter.setSizes([430, 220])
        layout.addWidget(splitter, 1)
        analysis_row = QHBoxLayout()
        health = QPushButton("模型健康诊断")
        health.clicked.connect(self._health_selected_documents)
        semantic_index = QPushButton("重建语义索引")
        semantic_index.clicked.connect(self.controller.rebuild_semantic_index_async)
        semantic_search = QPushButton("语义搜索")
        semantic_search.clicked.connect(self._semantic_search)
        analysis_row.addWidget(health)
        analysis_row.addWidget(semantic_index)
        analysis_row.addWidget(semantic_search)
        analysis_row.addStretch(1)
        layout.addLayout(analysis_row)
        return page

    def _build_automation_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 12, 18)
        title = QLabel("自动化中心")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)
        subtitle = QLabel("批任务保存在数据库中；程序中断后可回收租约并继续未完成项目。")
        subtitle.setProperty("class", "muted")
        layout.addWidget(subtitle)
        job_actions = QHBoxLayout()
        run_jobs = QPushButton("运行排队任务")
        run_jobs.setObjectName("primaryButton")
        run_jobs.clicked.connect(self.controller.run_queued_jobs_async)
        refresh_jobs = QPushButton("刷新任务")
        refresh_jobs.clicked.connect(self.controller.refresh_jobs)
        self.pause_job_button = QPushButton("暂停")
        self.pause_job_button.clicked.connect(self._pause_selected_job)
        self.resume_job_button = QPushButton("继续")
        self.resume_job_button.clicked.connect(self._resume_selected_job)
        self.cancel_job_button = QPushButton("取消")
        self.cancel_job_button.setObjectName("dangerButton")
        self.cancel_job_button.clicked.connect(self._cancel_selected_job)
        for button in (
            run_jobs,
            refresh_jobs,
            self.pause_job_button,
            self.resume_job_button,
            self.cancel_job_button,
        ):
            job_actions.addWidget(button)
        job_actions.addStretch(1)
        layout.addLayout(job_actions)

        learning_actions = QHBoxLayout()
        capability = QPushButton("运行版本能力实验")
        capability.clicked.connect(self.controller.run_capability_lab_async)
        start_demo = QPushButton("开始示教")
        start_demo.clicked.connect(self._start_demonstration)
        finish_demo = QPushButton("结束示教")
        finish_demo.clicked.connect(self._finish_demonstration)
        approve_demo = QPushButton("批准最近配方")
        approve_demo.clicked.connect(self._approve_demonstration)
        replay_demo = QPushButton("引导回放")
        replay_demo.clicked.connect(self._replay_demonstration)
        for button in (
            capability,
            start_demo,
            finish_demo,
            approve_demo,
            replay_demo,
        ):
            learning_actions.addWidget(button)
        learning_actions.addStretch(1)
        layout.addLayout(learning_actions)
        self.jobs_table = QTableWidget(0, 7)
        self.jobs_table.setHorizontalHeaderLabels(
            ["任务 ID", "类型", "状态", "进度", "成功", "失败", "消息"]
        )
        self.jobs_table.verticalHeader().hide()
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.jobs_table, 1)
        self.automation_detail = QTextEdit()
        self.automation_detail.setReadOnly(True)
        self.automation_detail.setPlaceholderText("版本能力实验和批任务结果显示在这里。")
        layout.addWidget(self.automation_detail, 1)
        return page

    def _build_history_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 12, 18)
        title = QLabel("历史与诊断")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["时间", "状态", "需求", "错误"])
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.history_table, 1)
        refresh = QPushButton("刷新历史")
        refresh.clicked.connect(self._refresh_history)
        layout.addWidget(refresh)
        return page

    def _build_activity_panel(self) -> QFrame:
        panel = QFrame()
        panel.setProperty("class", "panel")
        panel.setMinimumWidth(320)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        title = QLabel("活动与审计")
        title.setProperty("class", "sectionTitle")
        layout.addWidget(title)
        self.runtime_status = QLabel("等待任务")
        self.runtime_status.setWordWrap(True)
        self.runtime_status.setProperty("class", "muted")
        layout.addWidget(self.runtime_status)
        context_card, context_layout = self._card()
        context_title = QLabel("SolidWorks 实时上下文")
        context_title.setProperty("class", "sectionTitle")
        context_layout.addWidget(context_title)
        self.context_document = QLabel("文档：等待 Add-in")
        self.context_document.setWordWrap(True)
        context_layout.addWidget(self.context_document)
        self.context_selection = QLabel("选择：0 项")
        self.context_selection.setWordWrap(True)
        self.context_selection.setProperty("class", "muted")
        context_layout.addWidget(self.context_selection)
        self.context_state = QLabel("状态：未连接")
        self.context_state.setProperty("class", "muted")
        context_layout.addWidget(self.context_state)
        layout.addWidget(context_card)
        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.document().setMaximumBlockCount(3000)
        layout.addWidget(self.activity_log, 1)
        clear = QPushButton("清空界面日志")
        clear.clicked.connect(self.activity_log.clear)
        layout.addWidget(clear)
        return panel

    def _build_menus(self) -> None:
        file_menu = self.title_bar.menu.addMenu("文件")
        new_action = QAction("新任务", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._new_continuous_task)
        library_action = QAction("本地模型库", self)
        library_action.setShortcut("Ctrl+L")
        library_action.triggered.connect(lambda: self._navigate(1))
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)
        file_menu.addActions([new_action, library_action, exit_action])
        modeling_menu = self.title_bar.menu.addMenu("建模")
        task_action = QAction("持续建模任务", self)
        task_action.triggered.connect(lambda: self._navigate(0))
        self.legacy_plan_action = QAction("兼容固定计划", self)
        self.legacy_plan_action.triggered.connect(self._show_legacy_model_page)
        modeling_menu.addActions([task_action, self.legacy_plan_action])
        settings_menu = self.title_bar.menu.addMenu("设置")
        model_action = QAction("模型服务…", self)
        model_action.triggered.connect(self._open_settings)
        settings_menu.addAction(model_action)
        library_menu = self.title_bar.menu.addMenu("模型库")
        quality_action = QAction("检查引用与质量", self)
        quality_action.triggered.connect(self._analyze_selected_document)
        similar_action = QAction("查找相似模型", self)
        similar_action.triggered.connect(self._find_similar_documents)
        compare_action = QAction("对比两个参数快照", self)
        compare_action.triggered.connect(self._compare_selected_documents)
        health_action = QAction("模型健康诊断", self)
        health_action.triggered.connect(self._health_selected_documents)
        semantic_action = QAction("重建语义索引", self)
        semantic_action.triggered.connect(self.controller.rebuild_semantic_index_async)
        library_menu.addActions(
            [quality_action, health_action, similar_action, compare_action, semantic_action]
        )
        automation_menu = self.title_bar.menu.addMenu("自动化")
        jobs_action = QAction("自动化中心", self)
        jobs_action.triggered.connect(lambda: self._navigate(2))
        run_jobs_action = QAction("运行排队任务", self)
        run_jobs_action.triggered.connect(self.controller.run_queued_jobs_async)
        capability_action = QAction("版本能力实验", self)
        capability_action.triggered.connect(self.controller.run_capability_lab_async)
        automation_menu.addActions([jobs_action, run_jobs_action, capability_action])
        engineering_menu = self.title_bar.menu.addMenu("工程自动化")
        assembly_action = QAction("检查选中装配体", self)
        assembly_action.triggered.connect(self._inspect_selected_assembly)
        mate_action = QAction("用命名目标创建配合…", self)
        mate_action.triggered.connect(self._create_assembly_mate)
        drawing_action = QAction("为选中模型生成工程图…", self)
        drawing_action.triggered.connect(self._generate_selected_drawing)
        manufacturing_action = QAction("PMI 与 DFM 分析", self)
        manufacturing_action.triggered.connect(self._manufacturing_selected)
        pdm_action = QAction("读取 PDM 状态", self)
        pdm_action.triggered.connect(self._inspect_selected_pdm)
        engineering_menu.addActions(
            [
                assembly_action,
                mate_action,
                drawing_action,
                manufacturing_action,
                pdm_action,
            ]
        )
        help_menu = self.title_bar.menu.addMenu("帮助")
        log_action = QAction("显示日志位置", self)
        log_action.triggered.connect(
            lambda: QMessageBox.information(
                self,
                "日志位置",
                str(self.controller.paths.logs),
                QMessageBox.StandardButton.Ok,
            )
        )
        about = QAction("关于", self)
        about.triggered.connect(
            lambda: QMessageBox.information(
                self,
                "关于",
                "SolidWorks 智能建模助手 5.3.0\n受控交互建模、持久检查点恢复、写前校验与需求证据验收。",
                QMessageBox.StandardButton.Ok,
            )
        )
        help_menu.addActions([log_action, about])

    def _navigate(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == 0:
            self.model_pages.setCurrentIndex(0)
            self.task_panel.refresh_recent()
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)
        if index == 2:
            self.controller.refresh_jobs()
        elif index == 3:
            self._refresh_history()
        self._update_mode()

    def _show_legacy_model_page(self) -> None:
        self._navigate(0)
        self.model_pages.setCurrentIndex(1)
        self._update_mode()

    def _new_continuous_task(self) -> None:
        self._navigate(0)
        self.task_panel.new_draft()

    def _generate_plan(self) -> None:
        self._show_legacy_model_page()
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "缺少需求", "请先输入建模需求。")
            return
        self._set_clarification_visible(False)
        self.generate_button.setEnabled(False)
        self.generate_button.setText("正在生成…")
        self.stop_button.setEnabled(True)
        self._execution_buttons(False, preserve_stop=True)
        self._planning_active = True
        self._planning_started = time.monotonic()
        self.elapsed_timer.start()
        self._show_progress("计划生成进度", "正在整理需求和工具", state="running")
        self.controller.plan_async(prompt)

    def _execute_next(self) -> None:
        self._execution_buttons(False, preserve_stop=True)
        self.controller.execute_next()

    def _run_all(self) -> None:
        self._execution_buttons(False, preserve_stop=True)
        self.controller.run_all()

    def _on_plan_ready(self, plan: Plan) -> None:
        self.plan = plan
        self.failed_result = None
        self._render_plan_table()
        self.controller.load_plan(plan)
        self.generate_button.setEnabled(True)
        self.generate_button.setText("重新生成计划")
        self._execution_buttons(True)
        self._planning_active = False
        self.elapsed_timer.stop()
        self._show_progress(
            "计划生成完成",
            f"已通过工具与参数校验，共 {len(plan.steps)} 步",
            state="success",
            indeterminate=False,
        )

    def _render_plan_table(self, statuses: list[str] | None = None) -> None:
        if self.plan is None:
            self.step_status = []
            self.plan_table.setRowCount(0)
            self.plan_assumptions.clear()
            self.plan_assumptions.hide()
            return
        self.step_status = list(statuses or ["pending"] * len(self.plan.steps))
        self.plan_preview.set_plan(self.plan)
        self.plan_summary.setText(f"{len(self.plan.steps)} 步 · {self.plan.task}")
        if self.plan.assumptions:
            self.plan_assumptions.setText(
                "Agent 假设（执行前可审查）：\n• "
                + "\n• ".join(self.plan.assumptions)
            )
            self.plan_assumptions.show()
        else:
            self.plan_assumptions.clear()
            self.plan_assumptions.hide()
        self.plan_table.setRowCount(len(self.plan.steps))
        for index, call in enumerate(self.plan.steps):
            self.plan_table.setItem(index, 0, QTableWidgetItem(str(index + 1)))
            self._set_step_status(index, self.step_status[index])
            self.plan_table.setItem(index, 2, QTableWidgetItem(call.tool))
            self.plan_table.setItem(
                index,
                3,
                QTableWidgetItem(
                    json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
                ),
            )

    def _on_event(self, event: str, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        if event == "planning_started":
            self._append_log("计划", "规划请求已建立。")
        elif event == "planning_progress":
            message = str(data.get("message") or "正在处理")
            self.progress_detail.setText(message)
            self.runtime_status.setText(message)
            self._append_log("计划进度", message)
        elif event == "planning_clarification":
            self._show_legacy_model_page()
            self._planning_active = False
            self.elapsed_timer.stop()
            self._execution_buttons(False)
            self.generate_button.setEnabled(True)
            self.generate_button.setText("补充条件后重新生成")
            questions = [str(value) for value in data.get("questions", [])]
            detail = "\n".join(
                f"{index}. {question}" for index, question in enumerate(questions, 1)
            )
            self.progress_title.setText("需要补充建模条件")
            self.progress_detail.setText(detail or str(data.get("message") or ""))
            self.plan_progress.setRange(0, 100)
            self.plan_progress.setValue(0)
            self.runtime_status.setText("请在原需求中补充上述条件，然后重新生成计划。")
            self._append_log("需求待确认", detail)
            self._set_clarification_visible(True)
        elif event == "planning_failed":
            message = str(data.get("message") or "计划生成失败")
            self._planning_active = False
            self.elapsed_timer.stop()
            self.generate_button.setEnabled(True)
            self.generate_button.setText("重新生成计划")
            self._execution_buttons(False)
            self._show_progress("计划生成失败", message, state="failed", indeterminate=False)
            self.runtime_status.setText("计划生成失败；需求已保留，可处理后重试")
            self._append_log("计划失败", message)
            self._set_clarification_visible(False)
        elif event == "step_started":
            index = int(data.get("index", 0))
            self._set_step_status(index, "running")
            self._execution_buttons(False, preserve_stop=True)
            self.runtime_status.setText(f"正在执行第 {index + 1} 步")
            self._append_log("执行", json.dumps(data.get("call", {}), ensure_ascii=False))
        elif event in {"step_succeeded", "step_failed"}:
            index = int(data.get("index", 0))
            result = dict(data.get("result") or {})
            if event == "step_succeeded":
                self._set_step_status(index, "succeeded")
                self._execution_buttons(True)
                self._append_log("成功", str(result.get("message") or ""))
            else:
                self._set_step_status(index, "failed")
                self.failed_result = result
                self._execution_buttons(False)
                self._set_recovery_busy(False)
                self.recovery_message.setText(
                    f"第 {index + 1} 步 {result.get('call', {}).get('tool', '')}\n"
                    f"{result.get('message', '')}"
                )
                self.recovery_card.show()
                self.runtime_status.setText("执行已暂停，请选择恢复方式")
                self._append_log("失败", str(result.get("message") or ""))
        elif event in {"retry_ready", "failed_step_replaced"}:
            index = int(data.get("index", 0))
            self._set_step_status(index, "pending")
            self.recovery_card.hide()
            self._execution_buttons(True)
        elif event == "step_skipped":
            index = int(data.get("index", 0))
            self._set_step_status(index, "skipped")
            self.recovery_card.hide()
            self._execution_buttons(True)
        elif event == "manual_completion_verified":
            index = int(data.get("index", 0))
            self._set_step_status(index, "succeeded")
            self.recovery_card.hide()
            self._execution_buttons(True)
            self._append_log("人工验证", "人工操作已通过后验验证。")
        elif event == "manual_verification_failed":
            self.recovery_message.setText(str(data.get("message") or "人工验证未通过"))
            self._append_log("人工验证失败", str(data.get("message") or ""))
        elif event == "repair_started":
            self._set_recovery_busy(True)
            automatic = bool(data.get("automatic"))
            backend = str(data.get("agent_backend") or "")
            using_pi = backend in {"auto", "pi"}
            self.recovery_message.setText(
                "Pi Agent 正在结合本任务历次失败进行多轮恢复推理…"
                if using_pi
                else (
                    "正在自动分析失败并生成替代步骤…"
                    if automatic
                    else "正在把失败上下文交给已配置的模型服务…"
                )
            )
            self._append_log(
                "自动修复" if automatic else "计划修复",
                "已开始生成替代后缀。",
            )
        elif event == "repair_progress":
            message = str(data.get("message") or "正在修复计划")
            self.recovery_message.setText(message)
            self._append_log("计划修复", message)
        elif event == "repair_failed":
            message = str(data.get("message") or "模型没有生成可用的修复计划")
            self._set_recovery_busy(False)
            self.recovery_message.setText(message)
            self._append_log("计划修复失败", message)
        elif event == "pi_agent_fallback":
            message = str(data.get("message") or "Pi 不可用，已回退内置监督器。")
            self.recovery_message.setText(message)
            self._append_log("Pi 回退", message)
        elif event == "remaining_replaced":
            start = int(data.get("index", 0))
            calls = [ToolCall.from_mapping(value) for value in data.get("calls", [])]
            if self.plan is not None:
                prefix_statuses = self.step_status[:start]
                self.plan.steps[start:] = calls
                self._render_plan_table(prefix_statuses + ["pending"] * len(calls))
            self.failed_result = None
            self._set_recovery_busy(False)
            self.recovery_card.hide()
            self._execution_buttons(True)
            self.runtime_status.setText("模型已生成并校验替代步骤，可继续执行")
            self._append_log("计划修复", f"已替换为 {len(calls)} 个后续步骤。")
        elif event == "recovery_assessed":
            reason = str(data.get("reason") or "")
            category = str(data.get("category") or "unknown")
            if reason:
                self.recovery_message.setText(
                    self.recovery_message.text() + f"\n恢复分类：{category}；{reason}"
                )
            self._append_log("恢复判断", f"{category}：{reason}")
        elif event == "automatic_retry_scheduled":
            self._set_recovery_busy(True)
            self.recovery_message.setText("检测到暂时性故障，正在进行一次受控自动重试…")
            self._append_log("自动重试", "模型状态未变化，已安排一次受控重试。")
        elif event == "automatic_repair_applied":
            self._append_log(
                "自动修复",
                f"替代步骤已通过本地校验，共 {int(data.get('replacement_count', 0))} 步。",
            )
        elif event == "agent_review_required":
            message = str(data.get("reason") or "当前模型状态不适合自动修改。")
            self._set_recovery_busy(False)
            self.recovery_message.setText("监督 Agent 请求人工复核：" + message)
            self._append_log("监督 Agent 停止", message)
        elif event in {"automatic_repair_unavailable", "automatic_repair_exhausted"}:
            message = str(data.get("message") or "自动修复不可用。")
            self._set_recovery_busy(False)
            self.recovery_message.setText(message)
            self._append_log("自动修复停止", message)
        elif event == "plan_completed":
            self._execution_buttons(False)
            self.runtime_status.setText("计划执行完成，文档保持未保存")
            self._append_log("完成", "全部计划步骤已结束。")
        elif event in {"abort_requested", "all_cancel_requested"}:
            self._planning_active = False
            self.elapsed_timer.stop()
            self.generate_button.setEnabled(True)
            self.generate_button.setText("重新生成计划")
            self._execution_buttons(False)
            self.runtime_status.setText("已停止后续动作并中断自动化工作进程，请检查当前模型状态")
            self._append_log(
                "停止", "已中断助手进程；SolidWorks 已受理的当前原子操作可能已经完成。"
            )
        elif event == "runtime_error":
            self._append_log("运行错误", str(data.get("message") or ""))
            self.runtime_status.setText(str(data.get("message") or "运行错误"))
            if str(data.get("action") or "").endswith("selection_reference"):
                self._set_target_reference_busy(False)
        elif event == "selection_reference_saved":
            self._set_target_reference_busy(False)
            alias = str(data.get("alias") or "")
            self.runtime_status.setText(f"已保存命名目标：{alias}")
            self._append_log("命名目标", f"已保存 {alias}")
        elif event == "selection_reference_restored":
            self._set_target_reference_busy(False)
            alias = str(data.get("alias") or "")
            self.runtime_status.setText(f"已恢复命名目标：{alias}")
            self._append_log("命名目标", f"已恢复 {alias}")
        elif event == "selection_reference_deleted":
            self._set_target_reference_busy(False)
            self._append_log("命名目标", "已删除命名目标引用。")
        elif event == "selection_reference_failed":
            self._set_target_reference_busy(False)
            self.runtime_status.setText(str(data.get("message") or "命名目标操作失败"))
        elif event == "library_started":
            self._set_library_busy(True)
            self.library_stop.setEnabled(True)
            self.library_progress.setRange(0, 0)
            self.library_status.setText("任务已开始")
        elif event == "library_progress" and isinstance(payload, LibraryProgress):
            value = payload
            if value.stage == "discovering":
                self.library_progress.setRange(0, 0)
            else:
                self.library_progress.setRange(0, max(1, value.total))
                self.library_progress.setValue(value.processed)
            self.library_status.setText(
                f"{value.processed}/{value.total} · 成功 {value.succeeded} · "
                f"跳过 {value.skipped} · 失败 {value.failed} · "
                f"{Path(value.current_path).name} · {value.elapsed_seconds:.1f} 秒"
            )
        elif event == "library_finished":
            self._set_library_busy(False)
            self.library_stop.setEnabled(False)
            total = int(data.get("total", 0))
            self.library_progress.setRange(0, max(1, total))
            self.library_progress.setValue(int(data.get("processed", total)))
            if data.get("cancelled"):
                state = "已停止"
            elif data.get("incomplete"):
                state = "扫描不完整（未更新缺失状态）"
            else:
                state = "已完成"
            self.library_status.setText(
                f"{state} · 成功 {data.get('succeeded', 0)} · "
                f"跳过 {data.get('skipped', 0)} · 失败 {data.get('failed', 0)}"
            )
        elif event == "library_failed":
            self._set_library_busy(False)
            self.library_stop.setEnabled(False)
            self.library_status.setText(f"任务失败：{data.get('message', '')}")
            self._append_log("模型库失败", str(data.get("message") or ""))
        elif event == "library_rejected":
            self._append_log("模型库任务未启动", str(data.get("message") or ""))
        elif event == "derivation_started":
            self._set_library_busy(True)
            self.library_stop.setEnabled(True)
            self.library_progress.setRange(0, 0)
            strategy = str(data.get("strategy") or "")
            message = {
                "pack_and_go": "正在打包装配体及其引用…",
                "export": "正在使用 SolidWorks 导出副本…",
                "product_family": "正在隔离副本中创建并校验产品族配置…",
                "drawing": "正在生成并校验工程图…",
            }.get(strategy, "正在创建安全副本…")
            self.library_status.setText(message)
        elif event == "advanced_progress":
            message = str(data.get("message") or "正在处理")
            completed = int(data.get("completed") or 0)
            total = int(data.get("total") or 0)
            self.library_progress.setRange(0, max(1, total))
            self.library_progress.setValue(completed)
            self.library_status.setText(message)
        elif event == "derivation_succeeded":
            self._set_library_busy(False)
            self.library_stop.setEnabled(False)
            self.library_progress.setRange(0, 1)
            self.library_progress.setValue(1)
            destination = str(data.get("destination") or "")
            strategy = str(data.get("strategy") or "")
            label = {
                "pack_and_go": "装配体包已生成",
                "export": "导出文件已生成",
                "product_family": "产品族零件已生成",
                "drawing": "工程图已生成",
            }.get(strategy, "副本已生成")
            self.library_status.setText(f"{label}：{destination}")
            self._append_log(label, destination)
        elif event in {"derivation_failed", "derivation_cancelled"}:
            self._set_library_busy(False)
            self.library_stop.setEnabled(False)
            if event == "derivation_cancelled":
                self.library_status.setText("派生已取消")
                self._append_log("安全派生", "用户已停止任务。")
            else:
                message = str(data.get("message") or "安全派生失败")
                self.library_status.setText(f"副本生成失败：{message}")
                self._append_log("安全派生失败", message)
        elif event == "derivation_rejected":
            self._append_log("安全派生未启动", str(data.get("message") or ""))
        elif event == "analysis_started":
            self._set_library_busy(True)
            self.library_stop.setEnabled(True)
            self.library_progress.setRange(0, 0)
            labels = {
                "compare": "正在对比参数快照…",
                "dependencies": "正在检查引用与模型质量…",
                "similar": "正在计算相似模型…",
                "health": "正在执行模型健康诊断…",
                "semantic_reindex": "正在重建本地语义索引…",
                "semantic_search": "正在执行本地语义搜索…",
                "capability_lab": "正在运行版本能力实验…",
                "assembly_inspect": "正在检查装配体结构与干涉…",
                "assembly_mate": "正在通过持久引用创建配合…",
                "manufacturing": "正在读取 PMI 并执行 DFM 规则…",
                "pdm_read": "正在只读查询 PDM 状态…",
            }
            self.library_status.setText(labels.get(str(data.get("kind")), "正在分析…"))
        elif event == "analysis_succeeded":
            self._set_library_busy(False)
            self.library_stop.setEnabled(False)
            self.library_progress.setRange(0, 1)
            self.library_progress.setValue(1)
            analysis_result = data.get("result")
            if str(data.get("kind")) == "capability_lab":
                self.automation_detail.setPlainText(
                    json.dumps(analysis_result, ensure_ascii=False, indent=2)
                )
                self._navigate(2)
            else:
                self.library_detail.setPlainText(
                    json.dumps(analysis_result, ensure_ascii=False, indent=2)
                )
                self.library_status.setText("分析完成，结果已显示在详情区。")
        elif event in {"analysis_failed", "analysis_cancelled"}:
            self._set_library_busy(False)
            self.library_stop.setEnabled(False)
            message = str(data.get("message") or "")
            self.library_status.setText(
                "分析已取消" if event == "analysis_cancelled" else f"分析失败：{message}"
            )
        elif event == "analysis_rejected":
            self._append_log("分析任务未启动", str(data.get("message") or ""))
        elif event == "jobs_changed":
            self._on_jobs_changed(payload)
        elif event == "jobs_started":
            self.automation_detail.setPlainText("正在执行持久化批任务…")
        elif event == "jobs_finished":
            self.controller.refresh_jobs()
            self.automation_detail.setPlainText(
                f"排队任务执行结束；本次处理 {data.get('processed_jobs', 0)} 个任务。"
            )
        elif event == "jobs_failed":
            self.controller.refresh_jobs()
            self.automation_detail.setPlainText(f"持久化任务执行失败：{data.get('message', '')}")
        elif event == "jobs_rejected":
            self._append_log("自动化任务未启动", str(data.get("message") or ""))
        elif event == "demonstration_started":
            self.automation_detail.setPlainText(
                f"示教录制中：{data.get('name', '')}\n"
                "请在 SolidWorks 中完成人工操作，系统只记录命令和前后上下文。"
            )
        elif event == "demonstration_action":
            self._append_log(
                "示教",
                f"已记录命令 {data.get('command')}，序号 {data.get('sequence')}",
            )
        elif event == "demonstration_finished":
            self.latest_recipe_id = str(data.get("id") or "")
            self.automation_detail.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        elif event == "demonstration_approved":
            self.automation_detail.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            self._append_log("示教", "配方已人工批准。")
        elif event == "demonstration_replay_ready":
            self.automation_detail.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            self._append_log("示教", "已生成引导回放清单；每一步仍需人工确认目标。")
        elif event == "demonstration_replay_recorded":
            self.automation_detail.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        elif event == "demonstration_failed":
            self._append_log("示教失败", str(data.get("message") or ""))
        elif event == "legacy_migration_started":
            self.runtime_status.setText("正在只读导入旧版模型库与历史…")
            self._append_log("数据迁移", "已开始读取 old 中的旧版数据库。")
        elif event == "legacy_migration_completed":
            summary = (
                f"模型 {data.get('documents', 0)}，参数快照 {data.get('manifests', 0)}，"
                f"标签 {data.get('tags', 0)}，历史 {data.get('history', 0)}"
            )
            self.runtime_status.setText(f"旧版数据迁移完成：{summary}")
            self._append_log("数据迁移完成", summary)
            self._update_mode()
            self._refresh_history()
            if data.get("credentials_required"):
                self._show_progress(
                    "需要重新输入 API Key",
                    "模型服务地址和模型名已迁移，但旧密钥密文未复制。请在设置中重新输入。",
                    state="failed",
                    indeterminate=False,
                )
        elif event == "legacy_migration_failed":
            message = str(data.get("message") or "旧版数据迁移失败")
            self.runtime_status.setText(message)
            self._append_log("数据迁移失败", message)
        elif event == "settings_saved":
            self._update_mode()
            self._set_library_busy(False)
        elif event == "solidworks_event":
            event_name = str(data.get("name") or "unknown")
            event_detail = dict(data.get("payload") or {})
            self._append_log(
                "SolidWorks 事件",
                f"{event_name} · {json.dumps(event_detail, ensure_ascii=False, separators=(',', ':'))}",
            )
            if event_name == "bridge.connected":
                self.runtime_status.setText("SolidWorks 事件桥已连接")
            elif event_name == "bridge.disconnecting":
                self.runtime_status.setText("SolidWorks 事件桥已断开")
        elif event == "solidworks_capabilities":
            revision = str(data.get("solidworks_revision") or "未知")
            capabilities = list(data.get("capabilities") or [])
            available = sum(
                1
                for capability in capabilities
                if isinstance(capability, dict) and capability.get("available")
            )
            self._append_log(
                "版本能力矩阵",
                f"SolidWorks {revision}：{available}/{len(capabilities)} 项 API 可用",
            )

    def _show_progress(
        self, title: str, detail: str, *, state: str, indeterminate: bool = True
    ) -> None:
        self.progress_card.setProperty("state", state)
        self.progress_card.style().unpolish(self.progress_card)
        self.progress_card.style().polish(self.progress_card)
        self.progress_title.setText(title)
        self.progress_detail.setText(detail)
        self.plan_progress.setRange(0, 0 if indeterminate else 100)
        if not indeterminate:
            self.plan_progress.setValue(100 if state == "success" else 0)
        self.progress_card.show()

    def _set_clarification_visible(self, visible: bool) -> None:
        if visible:
            self.prompt.setEnabled(True)
            self.prompt.setReadOnly(False)
        self.clarification_input.setVisible(visible)
        self.clarification_submit_button.setVisible(visible)
        self.clarification_input.setEnabled(visible)
        self.clarification_submit_button.setEnabled(visible)
        if visible:
            self.clarification_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _submit_clarification(self) -> None:
        addition = self.clarification_input.toPlainText().strip()
        if not addition:
            self.runtime_status.setText("请先填写补充建模条件。")
            self.clarification_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        original = self.prompt.toPlainText().strip()
        combined = f"{original}\n补充条件：{addition}" if original else addition
        self.prompt.setPlainText(combined)
        self.clarification_input.clear()
        self._generate_plan()

    def _on_context(self, context: ContextSnapshot) -> None:
        document = dict(context.document)
        title = str(document.get("title") or "无活动文档")
        configuration = str(document.get("configuration") or "")
        document_type = {1: "零件", 2: "装配体", 3: "工程图"}.get(
            int(document.get("document_type") or 0), "未知"
        )
        suffix = f" · {configuration}" if configuration else ""
        self.context_document.setText(f"文档：{title} · {document_type}{suffix}")
        selected = [item.name or item.type_name for item in context.selections]
        selection_text = "、".join(selected[:3])
        if len(selected) > 3:
            selection_text += f" 等 {len(selected)} 项"
        self.context_selection.setText(
            f"选择：{selection_text}" if selection_text else "选择：0 项"
        )
        if context.command_in_progress:
            state = "SolidWorks 命令执行中，写操作锁定"
        elif context.is_stale():
            state = "上下文已过期，等待 Add-in 更新"
        else:
            state = f"已同步 · {len(context.capabilities)} 项能力"
        self._context_allows_execution = not (
            context.command_in_progress
            or context.is_stale()
        )
        self._apply_execution_buttons()
        self.context_state.setText("状态：" + state)

    def _set_library_busy(self, busy: bool) -> None:
        self.library_scan_button.setEnabled(not busy)
        self.metadata_button.setEnabled(not busy and self.controller.document_manager.configured)
        self.deep_button.setEnabled(not busy)
        self.copy_part_button.setEnabled(not busy)
        self.derive_part_button.setEnabled(not busy)
        self.product_family_button.setEnabled(not busy)
        self.pack_and_go_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy)

    def _set_target_reference_busy(self, busy: bool) -> None:
        self.capture_target_button.setEnabled(not busy)
        has_reference = self.target_references.currentData() is not None
        self.restore_target_button.setEnabled(not busy and has_reference)
        self.delete_target_button.setEnabled(not busy and has_reference)

    def _on_selection_references(self, records: object) -> None:
        current_id = self.target_references.currentData()
        self.target_references.clear()
        values = list(records) if isinstance(records, list) else []
        selected_index = -1
        for index, reference in enumerate(values):
            label = f"{reference.alias} — {reference.document_title or Path(reference.document_path).name}"
            if reference.object_name:
                label += f" · {reference.object_name}"
            self.target_references.addItem(label, reference.id)
            if reference.id == current_id:
                selected_index = index
        if selected_index >= 0:
            self.target_references.setCurrentIndex(selected_index)
        elif values:
            self.target_references.setCurrentIndex(0)
        self._set_target_reference_busy(False)

    def _capture_target_reference(self) -> None:
        alias, accepted = QInputDialog.getText(
            self, "保存命名目标", "名称（例如：安装面、主轴线、切除边）："
        )
        if accepted and alias.strip():
            self._set_target_reference_busy(True)
            self.controller.capture_selection_reference(alias)

    def _restore_target_reference(self) -> None:
        reference_id = self.target_references.currentData()
        if reference_id is not None:
            self._set_target_reference_busy(True)
            self.controller.restore_selection_reference(int(reference_id))

    def _delete_target_reference(self) -> None:
        reference_id = self.target_references.currentData()
        if reference_id is not None:
            self._set_target_reference_busy(True)
            self.controller.delete_selection_reference(int(reference_id))

    def _tick_elapsed(self) -> None:
        if self._planning_active:
            self.elapsed_label.setText(f"{int(time.monotonic() - self._planning_started)} 秒")

    def _set_step_status(self, row: int, status: str) -> None:
        if 0 <= row < len(self.step_status):
            self.step_status[row] = status
        text, color = STATUS[status]
        item = QTableWidgetItem(text)
        item.setForeground(color)
        self.plan_table.setItem(row, 1, item)

    def _execution_buttons(self, enabled: bool, *, preserve_stop: bool = False) -> None:
        self._execution_requested_enabled = enabled
        self._execution_preserve_stop = preserve_stop
        self._apply_execution_buttons()

    def _apply_execution_buttons(self) -> None:
        enabled = self._execution_requested_enabled and self._context_allows_execution
        self.next_button.setEnabled(enabled)
        self.run_button.setEnabled(enabled)
        self.stop_button.setEnabled(
            self._execution_requested_enabled or self._execution_preserve_stop
        )

    def _set_recovery_busy(self, busy: bool) -> None:
        for button in self.recovery_buttons:
            button.setEnabled(not busy)

    def _edit_failed(self) -> None:
        if not self.failed_result:
            return
        call = dict(self.failed_result.get("call") or {})
        editor = QPlainTextEdit(json.dumps(call.get("arguments", {}), ensure_ascii=False, indent=2))
        dialog = QMessageBox(self)
        dialog.setWindowTitle("编辑失败步骤参数")
        dialog.setText(f"工具：{call.get('tool', '')}")
        dialog_layout = dialog.layout()
        if not isinstance(dialog_layout, QGridLayout):
            raise RuntimeError("参数编辑对话框布局不可用。")
        dialog_layout.addWidget(editor, 1, 0, 1, dialog_layout.columnCount())
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if dialog.exec() != QMessageBox.StandardButton.Ok:
            return
        try:
            arguments = json.loads(editor.toPlainText())
            replacement = ToolCall(
                str(call.get("tool") or ""),
                dict(arguments),
                str(call.get("description") or ""),
                str(call.get("id") or ""),
            )
            self.controller.replace_failed(replacement)
        except Exception as exc:
            QMessageBox.warning(self, "参数无效", str(exc))

    def _skip_failed(self) -> None:
        answer = QMessageBox.warning(
            self,
            "跳过失败步骤",
            "后续步骤可能依赖当前步骤。确认跳过吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.skip_failed()

    def _new_task(self) -> None:
        self.controller.reset()
        self.plan = None
        self.step_status.clear()
        self.failed_result = None
        self.prompt.clear()
        self.plan_table.setRowCount(0)
        self.plan_preview.set_plan(None)
        self.plan_summary.setText("尚未生成计划")
        self.plan_assumptions.clear()
        self.plan_assumptions.hide()
        self.progress_card.hide()
        self.clarification_input.clear()
        self._set_clarification_visible(False)
        self.recovery_card.hide()
        self.generate_button.setText("让 Agent 生成并校验计划")
        self.generate_button.setEnabled(True)
        self._execution_buttons(False)

    def _choose_library_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择模型目录")
        if directory:
            self.library_root.setText(directory)

    def _scan_library(self) -> None:
        root = self.library_root.text().strip()
        if not root:
            QMessageBox.warning(self, "缺少目录", "请先选择扫描目录。")
            return
        self.controller.scan_library_async(root)

    def _deep_read(self) -> None:
        if not any(not record.missing for record in self.controller.library_repository.documents()):
            QMessageBox.information(
                self,
                "没有文件",
                "请先完成快速扫描。",
                QMessageBox.StandardButton.Ok,
            )
            return
        self.controller.deep_read_all_async()

    def _metadata_read(self) -> None:
        if not any(not record.missing for record in self.controller.library_repository.documents()):
            QMessageBox.information(
                self,
                "没有文件",
                "请先完成快速扫描。",
                QMessageBox.StandardButton.Ok,
            )
            return
        self.controller.metadata_read_all_async()

    def _refresh_library(self) -> None:
        self.controller.refresh_documents(self.library_search.text(), 0)

    def _previous_library_page(self) -> None:
        size = self.controller.settings.library_page_size
        self.controller.refresh_documents(
            self.library_search.text(), self.controller.library_offset - size
        )

    def _next_library_page(self) -> None:
        size = self.controller.settings.library_page_size
        self.controller.refresh_documents(
            self.library_search.text(), self.controller.library_offset + size
        )

    def _on_documents(self, records: object) -> None:
        self.documents = list(records) if isinstance(records, list) else []
        total = self.controller.library_total
        offset = self.controller.library_offset
        end = min(total, offset + len(self.documents))
        self.library_page.setText(f"{offset + 1 if total else 0}-{end} / {total}")
        self.library_previous.setEnabled(offset > 0)
        self.library_next.setEnabled(end < total)
        self.library_table.setRowCount(len(self.documents))
        for row, record in enumerate(self.documents):
            values = (
                record.name,
                record.document_type,
                "源文件缺失" if record.missing else record.parameter_status,
                record.manifest_sha256[:12],
                f"{record.size_bytes / 1024:.1f} KB",
                record.path,
            )
            for column, value in enumerate(values):
                self.library_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _show_document_detail(self) -> None:
        rows = self.library_table.selectionModel().selectedRows()
        if not rows:
            return
        record = self.documents[rows[0].row()]
        manifest = self.controller.library_repository.manifest(record.id)
        self.library_detail.setPlainText(
            json.dumps(manifest, ensure_ascii=False, indent=2) if manifest else "尚未读取完整参数。"
        )

    def _selected_document(self) -> DocumentRecord | None:
        rows = self.library_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(
                self,
                "未选择零件",
                "请先在列表中选择一个零件。",
                QMessageBox.StandardButton.Ok,
            )
            return None
        record = self.documents[rows[0].row()]
        if record.document_type != "part" or record.missing:
            QMessageBox.warning(
                self,
                "不支持的文件",
                "当前安全派生仅支持存在的零件文件；装配体必须使用 Pack and Go。",
            )
            return None
        return record

    def _copy_selected_part(self) -> None:
        record = self._selected_document()
        if record is None:
            return
        source = Path(record.path)
        suggested = str(source.with_name(source.stem + "-副本" + source.suffix))
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存零件副本", suggested, "SolidWorks 零件 (*.SLDPRT)"
        )
        if destination:
            self.controller.copy_part_async(record.path, destination)

    def _pack_and_go_selected_assembly(self) -> None:
        rows = self.library_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(
                self,
                "未选择装配体",
                "请先在列表中选择一个装配体。",
                QMessageBox.StandardButton.Ok,
            )
            return
        record = self.documents[rows[0].row()]
        if record.document_type != "assembly" or record.missing:
            QMessageBox.warning(
                self,
                "不支持的文件",
                "Pack and Go 只支持当前存在的装配体文件。",
            )
            return
        source = Path(record.path)
        parent = QFileDialog.getExistingDirectory(
            self,
            "选择打包输出位置",
            str(source.parent),
        )
        if not parent:
            return
        folder_name, accepted = QInputDialog.getText(
            self,
            "Pack and Go",
            "新建的打包目录名称：",
            text=f"{source.stem}-PackAndGo",
        )
        if not accepted or not folder_name.strip():
            return
        destination = Path(parent) / folder_name.strip()
        self.controller.pack_and_go_async(record.path, str(destination))

    def _export_selected_document(self) -> None:
        rows = self.library_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(
                self,
                "未选择文档",
                "请先在列表中选择需要导出的文档。",
                QMessageBox.StandardButton.Ok,
            )
            return
        record = self.documents[rows[0].row()]
        if record.missing:
            QMessageBox.warning(self, "文件缺失", "源文件已经不存在，无法导出。")
            return
        source = Path(record.path)
        if record.document_type == "drawing":
            suggested = str(source.with_suffix(".PDF"))
            file_filter = "PDF (*.PDF);;DXF (*.DXF);;DWG (*.DWG)"
        elif record.document_type in {"part", "assembly"}:
            suggested = str(source.with_suffix(".STEP"))
            file_filter = "STEP (*.STEP *.STP);;STL (*.STL);;IGES (*.IGES *.IGS)"
        else:
            QMessageBox.warning(self, "不支持的文件", "当前文档类型没有安全导出格式。")
            return
        destination, _ = QFileDialog.getSaveFileName(self, "导出文档副本", suggested, file_filter)
        if destination:
            self.controller.export_document_async(record.path, destination)

    def _analyze_selected_document(self) -> None:
        records = self._selected_library_records()
        if len(records) != 1:
            QMessageBox.information(
                self,
                "选择一个文档",
                "请只选择一个需要检查的文档。",
                QMessageBox.StandardButton.Ok,
            )
            return
        self.controller.inspect_dependencies_async(records[0].id)

    def _find_similar_documents(self) -> None:
        records = self._selected_library_records()
        if len(records) != 1:
            QMessageBox.information(
                self,
                "选择一个文档",
                "请只选择一个基准文档。",
                QMessageBox.StandardButton.Ok,
            )
            return
        self.controller.find_similar_async(records[0].id)

    def _compare_selected_documents(self) -> None:
        records = self._selected_library_records()
        if len(records) != 2:
            QMessageBox.information(
                self,
                "选择两个文档",
                "请按住 Ctrl 选择两个已读取参数的文档。",
                QMessageBox.StandardButton.Ok,
            )
            return
        self.controller.compare_documents_async(records[0].id, records[1].id)

    def _selected_library_records(self) -> list[DocumentRecord]:
        rows = sorted({index.row() for index in self.library_table.selectionModel().selectedRows()})
        return [self.documents[row] for row in rows if row < len(self.documents)]

    def _derive_selected_part(self) -> None:
        record = self._selected_document()
        if record is None:
            return
        manifest = self.controller.library_repository.manifest(record.id)
        dimensions = {
            str(item.get("name")): item.get("display_value_mm")
            for item in (manifest or {}).get("dimensions", [])
            if item.get("name") and item.get("display_value_mm") is not None
        }
        if not dimensions:
            QMessageBox.information(
                self,
                "缺少参数",
                "请先对该零件执行深度读取，确认可写尺寸后再派生。",
                QMessageBox.StandardButton.Ok,
            )
            return
        editor = QPlainTextEdit(json.dumps(dimensions, ensure_ascii=False, indent=2))
        dialog = QMessageBox(self)
        dialog.setWindowTitle("修改副本参数")
        dialog.setText("编辑需要写入副本的尺寸值，单位为毫米；可删除不需要修改的项目。")
        dialog_layout = dialog.layout()
        if not isinstance(dialog_layout, QGridLayout):
            raise RuntimeError("参数派生对话框布局不可用。")
        dialog_layout.addWidget(editor, 1, 0, 1, dialog_layout.columnCount())
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if dialog.exec() != QMessageBox.StandardButton.Ok:
            return
        try:
            changes = json.loads(editor.toPlainText())
            if not isinstance(changes, dict) or not changes:
                raise ValueError("至少保留一个要修改的参数。")
        except Exception as exc:
            QMessageBox.warning(self, "参数 JSON 无效", str(exc))
            return
        source = Path(record.path)
        suggested = str(source.with_name(source.stem + "-派生" + source.suffix))
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存参数派生零件", suggested, "SolidWorks 零件 (*.SLDPRT)"
        )
        if destination:
            self.controller.derive_part_async(record.path, destination, changes)

    def _generate_product_family(self) -> None:
        record = self._selected_document()
        if record is None:
            return
        manifest = self.controller.library_repository.manifest(record.id)
        dimensions = {
            str(item.get("name")): item.get("display_value_mm")
            for item in (manifest or {}).get("dimensions", [])
            if item.get("name") and item.get("display_value_mm") is not None
        }
        if not dimensions:
            QMessageBox.information(
                self,
                "缺少参数",
                "请先对该零件执行深度读取，再创建产品族配置。",
                QMessageBox.StandardButton.Ok,
            )
            return
        example = [
            {"name": "规格-A", "dimensions_mm": dimensions, "properties": {}},
            {"name": "规格-B", "dimensions_mm": dimensions, "properties": {}},
        ]
        editor = QPlainTextEdit(json.dumps(example, ensure_ascii=False, indent=2))
        editor.setMinimumSize(720, 420)
        dialog = QMessageBox(self)
        dialog.setWindowTitle("产品族配置")
        dialog.setText("编辑配置名称、毫米尺寸和配置专属属性。所有配置必须包含相同尺寸列。")
        dialog_layout = dialog.layout()
        if not isinstance(dialog_layout, QGridLayout):
            raise RuntimeError("产品族参数对话框布局不可用。")
        dialog_layout.addWidget(editor, 1, 0, 1, dialog_layout.columnCount())
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if dialog.exec() != QMessageBox.StandardButton.Ok:
            return
        try:
            rows = json.loads(editor.toPlainText())
            if not isinstance(rows, list):
                raise ValueError("产品族配置必须是 JSON 数组。")
            variants = tuple(
                FamilyVariant(
                    str(row.get("name") or ""),
                    {
                        str(name): float(value)
                        for name, value in dict(row.get("dimensions_mm") or {}).items()
                    },
                    {
                        str(name): str(value)
                        for name, value in dict(row.get("properties") or {}).items()
                    },
                )
                for row in rows
                if isinstance(row, dict)
            )
            if len(variants) != len(rows):
                raise ValueError("每个产品族配置都必须是对象。")
        except Exception as exc:
            QMessageBox.warning(self, "产品族 JSON 无效", str(exc))
            return
        source = Path(record.path)
        suggested = str(source.with_name(source.stem + "-产品族" + source.suffix))
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存产品族零件", suggested, "SolidWorks 零件 (*.SLDPRT)"
        )
        if not destination:
            return
        try:
            spec = ProductFamilySpec(record.path, destination, variants)
            spec.validate()
        except Exception as exc:
            QMessageBox.warning(self, "产品族配置无效", str(exc))
            return
        self.controller.generate_product_family_async(spec)

    def _health_selected_documents(self) -> None:
        records = self._selected_library_records()
        if not records:
            QMessageBox.information(
                self,
                "未选择模型",
                "请选择一个或多个已经读取参数的模型。",
                QMessageBox.StandardButton.Ok,
            )
            return
        if len(records) == 1:
            self.controller.diagnose_document_async(records[0].id)
            return
        job_id = self.controller.create_health_batch([record.id for record in records])
        self._navigate(2)
        self.automation_detail.setPlainText(
            f"已创建持久化健康诊断任务：{job_id}\n点击“运行排队任务”开始执行。"
        )

    def _semantic_search(self) -> None:
        query = self.library_search.text().strip()
        if not query:
            QMessageBox.information(
                self,
                "输入查询",
                "请在模型库搜索框输入材料、属性、特征、尺寸或用途关键词。",
                QMessageBox.StandardButton.Ok,
            )
            return
        self.controller.semantic_search_async(query)

    def _single_selected_record(self) -> DocumentRecord | None:
        records = self._selected_library_records()
        if len(records) != 1:
            QMessageBox.information(
                self,
                "选择一个模型",
                "请在模型库中只选择一个目标文档。",
                QMessageBox.StandardButton.Ok,
            )
            return None
        if records[0].missing:
            QMessageBox.warning(self, "文件缺失", "选中文档的源文件不存在。")
            return None
        return records[0]

    def _inspect_selected_assembly(self) -> None:
        record = self._single_selected_record()
        if record is None:
            return
        if record.document_type != "assembly":
            QMessageBox.warning(self, "不是装配体", "该功能只接受 SLDASM。")
            return
        self.controller.inspect_assembly_async(record.path)

    def _create_assembly_mate(self) -> None:
        record = self._single_selected_record()
        if record is None:
            return
        if record.document_type != "assembly":
            QMessageBox.warning(self, "不是装配体", "配合目标必须是 SLDASM。")
            return
        references = self.controller.library_repository.selection_references(record.path)
        if len(references) < 2:
            QMessageBox.information(
                self,
                "缺少命名目标",
                "请先在该装配体中分别选择两个面、轴或边，并在智能建模页保存为命名目标。",
                QMessageBox.StandardButton.Ok,
            )
            return
        labels = [
            f"{item.alias} · {item.object_name or item.selection_type}" for item in references
        ]
        first_label, accepted = QInputDialog.getItem(
            self, "创建配合", "第一个命名目标：", labels, 0, False
        )
        if not accepted:
            return
        second_labels = [label for label in labels if label != first_label]
        second_label, accepted = QInputDialog.getItem(
            self, "创建配合", "第二个命名目标：", second_labels, 0, False
        )
        if not accepted:
            return
        mate_type, accepted = QInputDialog.getItem(
            self,
            "创建配合",
            "配合类型：",
            ["coincident", "concentric", "parallel", "perpendicular", "distance", "angle"],
            0,
            False,
        )
        if not accepted:
            return
        first = references[labels.index(first_label)]
        second = references[labels.index(second_label)]
        answer = QMessageBox.warning(
            self,
            "确认创建未保存配合",
            "系统将创建配合、重建并把装配体保留为未保存状态，必须人工检查后自行保存。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.controller.create_assembly_mate_async(
            MateRequest(
                record.path,
                first.persistent_ref,
                second.persistent_ref,
                mate_type,
            )
        )

    def _generate_selected_drawing(self) -> None:
        record = self._single_selected_record()
        if record is None:
            return
        if record.document_type not in {"part", "assembly"}:
            QMessageBox.warning(self, "不支持的模型", "工程图源模型必须是零件或装配体。")
            return
        template, _ = QFileDialog.getOpenFileName(
            self,
            "选择工程图模板",
            str(Path(record.path).parent),
            "SolidWorks 工程图模板 (*.DRWDOT *.SLDDRW)",
        )
        if not template:
            return
        source = Path(record.path)
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存工程图",
            str(source.with_suffix(".SLDDRW")),
            "SolidWorks 工程图 (*.SLDDRW)",
        )
        if not destination:
            return
        views = (
            DrawingViewSpec("主视图", "front", x=0.08, y=0.20),
            DrawingViewSpec("俯视图", "top", x=0.18, y=0.20),
            DrawingViewSpec("等轴测", "isometric", x=0.28, y=0.20),
        )
        include_bom = record.document_type == "assembly"
        spec = DrawingSpec(
            record.path,
            destination,
            template,
            views,
            include_bom=include_bom,
            include_balloons=include_bom,
            import_model_dimensions=True,
        )
        try:
            spec.validate()
        except Exception as exc:
            QMessageBox.warning(self, "工程图参数无效", str(exc))
            return
        self.controller.generate_drawing_async(spec)

    def _manufacturing_selected(self) -> None:
        record = self._single_selected_record()
        if record is not None:
            self.controller.manufacturing_analysis_async(record.id)

    def _inspect_selected_pdm(self) -> None:
        record = self._single_selected_record()
        if record is not None:
            self.controller.inspect_pdm_async(record.path)

    def _selected_job_id(self) -> str:
        rows = self.jobs_table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.jobs_table.item(rows[0].row(), 0)
        return item.text() if item else ""

    def _pause_selected_job(self) -> None:
        if job_id := self._selected_job_id():
            self.controller.pause_job(job_id)

    def _resume_selected_job(self) -> None:
        if job_id := self._selected_job_id():
            self.controller.resume_job(job_id)

    def _cancel_selected_job(self) -> None:
        if job_id := self._selected_job_id():
            self.controller.cancel_job(job_id)

    def _on_jobs_changed(self, jobs: object) -> None:
        values = list(jobs) if isinstance(jobs, list) else []
        self.jobs_table.setRowCount(len(values))
        for row, job in enumerate(values):
            progress = f"{job.get('processed', 0)}/{job.get('total', 0)}"
            columns = (
                job.get("id", ""),
                job.get("kind", ""),
                job.get("state", ""),
                progress,
                job.get("succeeded", 0),
                job.get("failed", 0),
                job.get("message", ""),
            )
            for column, value in enumerate(columns):
                self.jobs_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _start_demonstration(self) -> None:
        name, accepted = QInputDialog.getText(self, "开始示教", "配方名称：", text="人工操作示教")
        if not accepted:
            return
        try:
            self.controller.start_demonstration(name)
        except Exception as exc:
            QMessageBox.warning(self, "无法开始示教", str(exc))

    def _finish_demonstration(self) -> None:
        try:
            result = self.controller.finish_demonstration()
            self.latest_recipe_id = str(result.get("id") or "")
        except Exception as exc:
            QMessageBox.warning(self, "无法结束示教", str(exc))

    def _approve_demonstration(self) -> None:
        if not self.latest_recipe_id:
            QMessageBox.information(
                self,
                "没有待审配方",
                "请先结束一次示教并检查生成的配方。",
                QMessageBox.StandardButton.Ok,
            )
            return
        answer = QMessageBox.question(
            self,
            "批准示教配方",
            "批准后配方才可用于上下文匹配。仍需在每次执行前确认目标引用。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.controller.approve_demonstration_recipe(self.latest_recipe_id)
            except Exception as exc:
                QMessageBox.warning(self, "批准失败", str(exc))

    def _replay_demonstration(self) -> None:
        if not self.latest_recipe_id:
            QMessageBox.information(
                self,
                "没有可用配方",
                "请先完成并批准一次示教。",
                QMessageBox.StandardButton.Ok,
            )
            return
        try:
            replay = self.controller.prepare_demonstration_replay(self.latest_recipe_id)
        except Exception as exc:
            QMessageBox.warning(self, "无法引导回放", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "引导回放",
            f"已生成 {len(replay.get('steps', []))} 个步骤。请按右侧清单在 SolidWorks 中操作，"
            "并逐步确认目标。完成后是否记录为成功？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        try:
            self.controller.record_demonstration_replay_result(
                self.latest_recipe_id,
                succeeded=answer == QMessageBox.StandardButton.Yes,
            )
        except Exception as exc:
            QMessageBox.warning(self, "记录回放结果失败", str(exc))

    def _refresh_history(self) -> None:
        records = self.controller.history.recent()
        self.history_table.setRowCount(len(records))
        for row, record in enumerate(records):
            for column, key in enumerate(("updated_at", "state", "prompt", "error")):
                self.history_table.setItem(row, column, QTableWidgetItem(str(record.get(key, ""))))

    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            self.controller.settings, self.controller.settings_store.warning, self
        )
        if not dialog.exec():
            return
        try:
            self.controller.save_settings(dialog.value())
            self._append_log("设置", "模型服务设置已使用 Windows DPAPI 加密保存。")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def _update_mode(self) -> None:
        model = self.controller.settings.model
        model_pages = getattr(self, "model_pages", None)
        legacy = model_pages is not None and model_pages.currentIndex() == 1
        if legacy:
            text = f"兼容计划：模型 {model.model}" if model.configured else "兼容计划：本地规则规划器"
        elif not model.configured:
            text = "持续任务：模型服务未配置\n请打开“设置 → 模型服务”。"
        elif model.credential_unreadable:
            text = "持续任务：模型凭据需要更新\n请在模型服务中重新填写 API Key。"
        else:
            backend = {
                "auto": "自动（优先 Pi）",
                "pi": "Pi 持续会话",
                "pydantic": "内置持续会话",
            }.get(self.controller.settings.agent_backend, self.controller.settings.agent_backend)
            text = f"持续任务：{model.model}\n后端设置：{backend}"
        self.mode_label.setText(text)

    def _append_log(self, category: str, message: str) -> None:
        safe_category = html.escape(str(category))
        safe_message = html.escape(str(message)).replace("\n", "<br>")
        self.activity_log.append(f"<b>[{safe_category}]</b> {safe_message}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.controller.shutdown()
        event.accept()
