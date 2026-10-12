"""Render the real Windows Qt interface with isolated application data."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["SW_ASSISTANT_DATA_DIR"] = str(ROOT / "artifacts" / "v5-ui" / "data")
os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from sw_assistant.runtime import ApplicationController
from sw_assistant.ui import APP_STYLE, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    controller = ApplicationController()
    window = MainWindow(controller)
    window.show()
    output = ROOT / "artifacts" / "v5-ui"
    output.mkdir(parents=True, exist_ok=True)

    def capture():
        window.grab().save(str(output / "main.png"))
        snapshots = list((ROOT / "artifacts" / "v5-live-smoke" / "tasks").glob("*.json"))
        if snapshots:
            latest = max(snapshots, key=lambda path: path.stat().st_mtime)
            value = json.loads(latest.read_text("utf-8"))
            window.task_panel.render_snapshot({**value, "busy": False})
            app.processEvents()
            window.grab().save(str(output / "task.png"))
        print(str(output / "main.png"), flush=True)
        window.close()
        app.quit()

    QTimer.singleShot(2500, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
