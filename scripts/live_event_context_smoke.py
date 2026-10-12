from __future__ import annotations

import argparse
import json
import time

import pythoncom
import win32com.client
from win32com.client import VARIANT


def _value(target, name: str, *arguments):
    member = getattr(target, name)
    return member(*arguments) if callable(member) else member


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recover-process-id", type=int, default=0)
    parser.add_argument("--recover-title", default="")
    arguments = parser.parse_args()
    pythoncom.CoInitialize()
    app = None
    document = None
    title = ""
    try:
        app = win32com.client.GetActiveObject("SldWorks.Application")
        existing = app.ActiveDoc
        if existing is not None:
            existing_path = str(_value(existing, "GetPathName") or "")
            existing_title = str(_value(existing, "GetTitle") or "")
            existing_save_pending = bool(_value(existing, "GetSaveFlag"))
            process_id = int(app.GetProcessID())
            recovery_matches = (
                arguments.recover_process_id == process_id
                and arguments.recover_title == existing_title
                and not existing_path
                and not existing_save_pending
            )
            if not recovery_matches:
                raise RuntimeError("存在活动文档，且未通过 PID、标题、路径和保存状态的恢复校验。")
            app.CloseDoc(existing_title)
        app.NewPart()
        document = app.ActiveDoc
        if document is None:
            raise RuntimeError("SolidWorks 未创建临时零件。")
        title = str(_value(document, "GetTitle") or "")
        selected = False
        null_dispatch = VARIANT(pythoncom.VT_DISPATCH, None)
        for name in ("Front Plane", "前视基准面", "前基准面"):
            document.ClearSelection2(True)
            if document.Extension.SelectByID2(
                name, "PLANE", 0.0, 0.0, 0.0, False, 0, null_dispatch, 0
            ):
                selected = True
                break
        document.ForceRebuild3(False)
        document.ClearSelection2(True)
        time.sleep(1.0)
        print(
            json.dumps(
                {
                    "created": True,
                    "title": title,
                    "plane_selected": selected,
                    "saved": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        if app is not None and title:
            try:
                app.CloseDoc(title)
            except Exception:
                pass
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main())
