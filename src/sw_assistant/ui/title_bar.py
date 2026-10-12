from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QMenuBar, QToolButton, QWidget


class TitleBar(QWidget):
    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self._host_window = window
        self._drag_origin: QPoint | None = None
        self.setObjectName("titleBar")
        self.setFixedHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(10)
        brand = QLabel("SolidWorks 智能建模助手")
        brand.setObjectName("brandTitle")
        layout.addWidget(brand)
        subtitle = QLabel("5.3.0 · 交互建模 · 检查点恢复")
        subtitle.setObjectName("brandSubtitle")
        layout.addWidget(subtitle)
        self.menu = QMenuBar()
        layout.addWidget(self.menu)
        layout.addStretch(1)
        self.minimize = self._button("—", window.showMinimized)
        self.maximize = self._button("□", self._toggle_maximize)
        self.close_button = self._button("×", window.close, "closeButton")
        layout.addWidget(self.minimize)
        layout.addWidget(self.maximize)
        layout.addWidget(self.close_button)

    def _button(self, text: str, callback, name: str = "") -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setProperty("class", "windowButton")
        button.setObjectName(name)
        button.setFixedSize(36, 30)
        button.clicked.connect(callback)
        return button

    def _toggle_maximize(self) -> None:
        if self._host_window.isMaximized():
            self._host_window.showNormal()
        else:
            self._host_window.showMaximized()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = (
                event.globalPosition().toPoint() - self._host_window.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._host_window.isMaximized():
                self._host_window.showNormal()
            self._host_window.move(event.globalPosition().toPoint() - self._drag_origin)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_origin = None

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()
