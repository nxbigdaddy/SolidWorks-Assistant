from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.solidworks.gateway import SolidWorksGateway, _items, _value


def _views(document):
    output = {}
    for interface in ("raw", "DrawingDoc", "IDrawingDoc"):
        current = document
        error = ""
        if interface != "raw":
            try:
                from win32com.client import CastTo

                current = CastTo(document, interface)
            except Exception as exc:
                error = f"cast: {type(exc).__name__}: {exc}"
        names = []
        if not error:
            try:
                view = current.GetFirstView()
                for _ in range(100):
                    if view is None:
                        break
                    names.append(str(view.GetName2()))
                    view = view.GetNextView()
            except Exception as exc:
                error = f"read: {type(exc).__name__}: {exc}"
        output[interface] = {"names": names, "error": error}
    return output


def _query_interface(document):
    try:
        import pythoncom
        from win32com.client import gencache

        interface_id = "{83A33D33-27C5-11CE-BFD4-00400513BB57}"
        queried = document._oleobj_.QueryInterface(pythoncom.MakeIID(interface_id))
        wrapper_class = gencache.GetClassForCLSID(interface_id)
        wrapped = wrapper_class(queried)
        return {
            "ok": True,
            "wrapper": str(type(wrapped)),
            "has_get_first_view": hasattr(wrapped, "GetFirstView"),
            "has_insert_bom4": hasattr(wrapped, "InsertBomTable4"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("template")
    parser.add_argument("destination")
    args = parser.parse_args()
    destination = Path(args.destination).resolve(strict=False)
    if destination.exists():
        raise RuntimeError("目标文件已存在。")
    gateway = SolidWorksGateway(visible=True)
    drawing = model_document = None
    drawing_title = model_title = ""
    try:
        app = gateway.connect()
        drawing = app.NewDocument(str(Path(args.template).resolve(strict=True)), 0, 0.0, 0.0)
        drawing_title = str(_value(drawing, "GetTitle", "") or "")
        model_document, _ = gateway._open_readonly_document(args.model)
        model_title = str(_value(model_document, "GetTitle", "") or "")
        model_views = [str(item) for item in _items(_value(model_document, "GetModelViewNames"))]
        app.ActivateDoc3(drawing_title, False, 0, 0)
        orientation = gateway._drawing_orientation_names(model_views)["front"]
        live_view = drawing.CreateDrawViewFromModelView3(
            str(Path(args.model).resolve(strict=True)), orientation, 0.08, 0.18, 0.0
        )
        live_view.SetName2("主视图")
        before = _views(drawing)
        queried_before = _query_interface(drawing)
        null_dispatch = gateway._null_dispatch()
        errors = gateway._int_reference()
        warnings = gateway._int_reference()
        saved = drawing.Extension.SaveAs3(
            str(destination),
            0,
            1,
            null_dispatch,
            null_dispatch,
            errors,
            warnings,
        )
        after_save = _views(drawing)
        current_title = str(_value(drawing, "GetTitle", "") or drawing_title)
        app.CloseDoc(current_title)
        drawing = None
        reopened, _ = gateway._open_readonly_document(str(destination))
        reopened_title = str(_value(reopened, "GetTitle", "") or "")
        after_reopen = _views(reopened)
        app.CloseDoc(reopened_title)
        print(
            json.dumps(
                {
                    "saved": bool(saved),
                    "errors": repr(errors.value),
                    "warnings": repr(warnings.value),
                    "drawing_title_before": drawing_title,
                    "drawing_title_after": current_title,
                    "model_views": model_views,
                    "before": before,
                    "queried_before": queried_before,
                    "after_save": after_save,
                    "after_reopen": after_reopen,
                    "destination_exists": destination.is_file(),
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        if gateway._sw is not None:
            for title in (drawing_title, model_title):
                if title:
                    try:
                        gateway._sw.CloseDoc(title)
                    except Exception:
                        pass
        gateway.release(close_owned=True)


if __name__ == "__main__":
    raise SystemExit(main())
