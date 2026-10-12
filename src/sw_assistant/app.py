from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .runtime import ApplicationController
from .ui import APP_STYLE, MainWindow


def run(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("SolidWorks 智能建模助手")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    controller = ApplicationController()
    window = MainWindow(controller)
    window.show()
    candidates = (Path.cwd() / "old", Path(__file__).resolve().parents[2] / "old")
    archive = next((path for path in candidates if path.is_dir()), None)
    if archive is not None:
        QTimer.singleShot(0, lambda: controller.migrate_legacy_async(archive))
    return app.exec()
