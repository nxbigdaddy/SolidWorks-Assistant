from __future__ import annotations

from copy import deepcopy

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..infrastructure import AppSettings


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, warning: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("系统设置")
        self.setMinimumWidth(620)
        self._settings = deepcopy(settings)
        layout = QVBoxLayout(self)
        title = QLabel("模型服务")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)
        hint = QLabel(
            warning
            or "地址和模型留空时使用本地规则规划器；无需认证的本地服务可不填写 API Key。"
        )
        hint.setWordWrap(True)
        hint.setProperty("class", "muted")
        layout.addWidget(hint)
        form = QFormLayout()
        self.api_base = QLineEdit(settings.model.api_base)
        self.api_base.setPlaceholderText("https://example.com/v1")
        self.model = QLineEdit(settings.model.model)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("留空保留现有 Key；旧值不会在界面中显示")
        self.clear_key = QCheckBox("清除已保存的 API Key")
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setValue(settings.model.timeout_seconds)
        form.addRow("API 地址", self.api_base)
        form.addRow("模型名称", self.model)
        form.addRow("API Key", self.api_key)
        form.addRow("", self.clear_key)
        form.addRow("超时（秒）", self.timeout)
        self.agent_enabled = QCheckBox("启用需求规划 + 失败恢复的全流程 Agent")
        self.agent_enabled.setChecked(settings.agent_enabled)
        self.agent_max_model_requests = QSpinBox()
        self.agent_max_model_requests.setRange(2, 8)
        self.agent_max_model_requests.setValue(settings.agent_max_model_requests)
        self.agent_max_model_requests.setSuffix(" 次")
        form.addRow("全流程 Agent", self.agent_enabled)
        form.addRow("单次请求上限", self.agent_max_model_requests)
        self.agent_backend = QComboBox()
        self.agent_backend.addItem("自动（优先 Pi，失败时回退）", "auto")
        self.agent_backend.addItem("Pi 持久多轮 Agent", "pi")
        self.agent_backend.addItem("内置 PydanticAI", "pydantic")
        backend_index = self.agent_backend.findData(settings.agent_backend)
        self.agent_backend.setCurrentIndex(max(0, backend_index))
        self.pi_max_repair_rounds = QSpinBox()
        self.pi_max_repair_rounds.setRange(2, 12)
        self.pi_max_repair_rounds.setValue(settings.pi_max_repair_rounds)
        self.pi_max_repair_rounds.setSuffix(" 轮/任务")
        form.addRow("失败恢复后端", self.agent_backend)
        form.addRow("Pi 修正预算", self.pi_max_repair_rounds)
        pi_hint = QLabel(
            "Pi 仅负责持续推理；文件、命令、DLL 与 SolidWorks 工具均被禁用。"
            "自动模式在本机没有 Pi 或启动失败时回退到内置监督器。"
        )
        pi_hint.setWordWrap(True)
        pi_hint.setProperty("class", "muted")
        form.addRow("", pi_hint)
        self.auto_repair_failures = QCheckBox("执行失败后自动请求模型生成替代步骤")
        self.auto_repair_failures.setChecked(settings.auto_repair_failures)
        self.auto_resume_after_repair = QCheckBox("“执行全部”模式下，修复通过校验后自动继续")
        self.auto_resume_after_repair.setChecked(settings.auto_resume_after_repair)
        form.addRow("自动恢复", self.auto_repair_failures)
        form.addRow("", self.auto_resume_after_repair)
        document_manager_title = QLabel("SolidWorks Document Manager")
        document_manager_title.setProperty("class", "sectionTitle")
        form.addRow(document_manager_title)
        self.document_manager_key = QLineEdit()
        self.document_manager_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.document_manager_key.setPlaceholderText(
            "可选；留空保留现有许可证密钥"
        )
        self.clear_document_manager_key = QCheckBox(
            "清除已保存的 Document Manager 密钥"
        )
        form.addRow("许可证密钥", self.document_manager_key)
        form.addRow("", self.clear_document_manager_key)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> AppSettings:
        self._settings.model.api_base = self.api_base.text().strip()
        self._settings.model.model = self.model.text().strip()
        if self.api_key.text():
            self._settings.model.api_key = self.api_key.text()
            self._settings.model.credential_unreadable = False
        elif self.clear_key.isChecked():
            self._settings.model.api_key = ""
            self._settings.model.credential_unreadable = False
        self._settings.model.timeout_seconds = self.timeout.value()
        self._settings.agent_enabled = self.agent_enabled.isChecked()
        self._settings.agent_max_model_requests = self.agent_max_model_requests.value()
        self._settings.agent_backend = str(self.agent_backend.currentData())
        self._settings.pi_max_repair_rounds = self.pi_max_repair_rounds.value()
        self._settings.auto_repair_failures = self.auto_repair_failures.isChecked()
        self._settings.auto_resume_after_repair = self.auto_resume_after_repair.isChecked()
        if self.document_manager_key.text():
            self._settings.document_manager_key = self.document_manager_key.text().strip()
            self._settings.document_manager_credential_unreadable = False
        elif self.clear_document_manager_key.isChecked():
            self._settings.document_manager_key = ""
            self._settings.document_manager_credential_unreadable = False
        self._settings.model.validate()
        return self._settings
