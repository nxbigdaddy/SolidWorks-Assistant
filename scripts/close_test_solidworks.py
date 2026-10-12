from __future__ import annotations

import argparse
import json

import pythoncom
import win32com.client


def _value(target, name: str):
    member = getattr(target, name)
    return member() if callable(member) else member


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process-id", type=int, required=True)
    arguments = parser.parse_args()
    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("SldWorks.Application")
        actual_process_id = int(_value(app, "GetProcessID"))
        if actual_process_id != arguments.process_id:
            raise RuntimeError(
                f"SolidWorks PID 不匹配：expected={arguments.process_id}, actual={actual_process_id}"
            )
        document = app.ActiveDoc
        if document is not None:
            raise RuntimeError(
                "SolidWorks 仍有活动文档，安全清理拒绝退出应用程序。"
            )
        revision = str(_value(app, "RevisionNumber") or "")
        _value(app, "ExitApp")
        print(
            json.dumps(
                {
                    "closed": True,
                    "process_id": actual_process_id,
                    "revision": revision,
                    "active_document": False,
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main())
