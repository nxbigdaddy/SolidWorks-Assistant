from __future__ import annotations

import json

import pythoncom
import win32com.client


def _value(target, name: str):
    member = getattr(target, name)
    return member() if callable(member) else member


def main() -> int:
    pythoncom.CoInitialize()
    try:
        app = win32com.client.GetActiveObject("SldWorks.Application")
        document = app.ActiveDoc
        print(
            json.dumps(
                {
                    "process_id": int(app.GetProcessID()),
                    "title": str(_value(document, "GetTitle") or "") if document else "",
                    "path": str(_value(document, "GetPathName") or "") if document else "",
                    "save_pending": bool(_value(document, "GetSaveFlag")) if document else False,
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main())
