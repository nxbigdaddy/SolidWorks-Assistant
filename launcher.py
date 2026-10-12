from __future__ import annotations

import ctypes
import sys
from pathlib import Path


REQUIRED_FROZEN_RUNTIME_FILES = (
    "_internal/python3.dll",
    "_internal/python312.dll",
    "_internal/shiboken6/shiboken6.abi3.dll",
    "_internal/PySide6/QtCore.pyd",
    "_internal/PySide6/Qt6Core.dll",
    "_internal/PySide6/QtGui.pyd",
    "_internal/PySide6/Qt6Gui.dll",
    "_internal/PySide6/QtWidgets.pyd",
    "_internal/PySide6/Qt6Widgets.dll",
    "_internal/PySide6/plugins/platforms/qwindows.dll",
)

INCOMPATIBLE_FROZEN_RUNTIME_FILES = (
    "_internal/icuuc.dll",
)


def missing_frozen_runtime_files(application_root: str | Path) -> list[str]:
    root = Path(application_root)
    return [
        relative
        for relative in REQUIRED_FROZEN_RUNTIME_FILES
        if not (root / Path(relative)).is_file()
    ]


def incompatible_frozen_runtime_files(application_root: str | Path) -> list[str]:
    root = Path(application_root)
    return [
        relative
        for relative in INCOMPATIBLE_FROZEN_RUNTIME_FILES
        if (root / Path(relative)).is_file()
    ]


def _validate_frozen_bundle() -> None:
    if not bool(getattr(sys, "frozen", False)):
        return
    root = Path(sys.executable).resolve().parent
    missing = missing_frozen_runtime_files(root)
    incompatible = incompatible_frozen_runtime_files(root)
    if not missing and not incompatible:
        return
    details: list[str] = []
    if missing:
        details.append("缺失文件：\n- " + "\n- ".join(missing[:10]))
    if incompatible:
        details.append(
            "发现与 Qt 冲突的运行库：\n- " + "\n- ".join(incompatible[:10])
        )
    message = (
        "程序目录中的 Qt/Python 运行环境无效，无法启动。\n\n"
        "请使用与当前版本配套的安装程序，或完整解压 portable.zip 后运行；"
        "不要运行 build\\SolidWorksAssistant 中的 PyInstaller 中间 EXE，"
        "也不要只复制 SolidWorksAssistant.exe。\n\n"
        + "\n\n".join(details)
    )
    try:
        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None,
            message,
            "SolidWorks AI Assistant - 安装不完整",
            0x10,
        )
    except Exception:
        pass
    raise SystemExit(message)


_validate_frozen_bundle()

from sw_assistant.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
