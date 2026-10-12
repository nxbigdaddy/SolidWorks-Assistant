from __future__ import annotations

import base64
import glob
import hashlib
import json
import math
import os
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..domain import ModelSnapshot, ToolCall, ToolError, utc_now
from ..tools import bound_investigation_result
from .checkpoints import CandidateWorkspaceMixin
from .topology import (
    PlanarFaceSignature,
    SketchLineSignature,
    SketchSessionSignature,
    TopologyResolutionError,
    capture_sketch_line_signature,
    capture_sketch_session_signature,
    capture_terminal_face_signature,
    resolve_planar_face,
    resolve_sketch_line,
    validate_sketch_session,
)


PLANE_NAMES = {
    "front": ("Front Plane", "前视基准面", "前基准面"),
    "top": ("Top Plane", "上视基准面", "上基准面"),
    "right": ("Right Plane", "右视基准面", "右基准面"),
}
PLANE_ORDINAL = {"front": 0, "top": 1, "right": 2}
PLANE_FRAMES = {
    "front": {
        "origin": (0.0, 0.0, 0.0),
        "origin_xy": (0.0, 0.0),
        "u": (1.0, 0.0, 0.0),
        "v": (0.0, 1.0, 0.0),
        "normal": (0.0, 0.0, 1.0),
    },
    "top": {
        "origin": (0.0, 0.0, 0.0),
        "origin_xy": (0.0, 0.0),
        "u": (1.0, 0.0, 0.0),
        "v": (0.0, 0.0, -1.0),
        "normal": (0.0, 1.0, 0.0),
    },
    "right": {
        "origin": (0.0, 0.0, 0.0),
        "origin_xy": (0.0, 0.0),
        # Keep the public right-plane coordinates in ascending world-axis
        # order: local x -> world Y and local y -> world Z.  This convention is
        # considerably less error-prone for generated plans than the former
        # screen-oriented (local x -> -Z, local y -> Y) frame.
        "u": (0.0, 1.0, 0.0),
        "v": (0.0, 0.0, 1.0),
        "normal": (1.0, 0.0, 0.0),
    },
}
DOC_TYPES = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}
SW_END_BLIND = 0
SW_END_MID_PLANE = 6
SKETCH_FEATURE_USAGE = {
    ("extrude", "boss"): (3, "boss-extrude"),
    ("extrude", "cut"): (11, "cut-extrude"),
    ("revolve", "boss"): (8, "boss-revolve"),
    ("revolve", "cut"): (13, "cut-revolve"),
}
SKETCH_CHECK_STATUS = {
    -1: "未知错误",
    1: "轮廓自相交",
    2: "草图实体自相交",
    3: "草图包含无效实体",
    4: "一个端点被错误地共享给多个实体",
    5: "草图为空",
    6: "草图包含开放轮廓",
    7: "轮廓数量不符合要求",
    8: "草图包含零长度实体",
    9: "草图包含多条开放轮廓",
    11: "草图混合了开放和闭合轮廓",
    12: "草图轮廓彼此相交",
    13: "草图包含不允许的分离轮廓",
    14: "特征需要闭合轮廓，但轮廓是开放的",
    16: "草图包含双重嵌套轮廓",
    21: "旋转草图缺少中心线",
    22: "选定轮廓开放或含义不明确",
    23: "旋转轮廓穿过或孤立接触中心线",
}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    try:
        member = getattr(obj, name)
        if hasattr(member, "_oleobj_"):
            return member
        return member() if callable(member) else member
    except Exception:
        return default


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return list(value)
    return [value]


def _created(value: Any) -> bool:
    """COM dispatch objects are valid results even when bool(value) is false."""

    return value is not None and value is not False


def _read_member(obj: Any, name: str, *args: Any) -> Any:
    """Strict read for investigator tools; all member names are local constants."""
    member = getattr(obj, name)
    if hasattr(member, "_oleobj_"):
        return member
    return member(*args) if callable(member) else member


class _ReadProbe:
    def __init__(self) -> None:
        self.issues: list[str] = []

    def issue(self, message: str) -> None:
        if len(self.issues) < 32:
            self.issues.append(message[:350])

    def attempt(self, label: str, operation: Any) -> Any:
        try:
            return operation()
        except Exception as exc:
            self.issue(f"{label}: {type(exc).__name__}")
            return None

    def get(self, obj: Any, name: str, *args: Any) -> Any:
        return self.attempt(name, lambda: _read_member(obj, name, *args))

    def finish(self, data: dict[str, Any]) -> dict[str, Any]:
        return bound_investigation_result({**data, "complete": not self.issues, "issues": self.issues})


class CircleEditError(ToolError):
    """A bounded edit failed; the worker must preserve its uncertainty evidence."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


class PolygonCreationError(ToolError):
    """Polygon creation did not prove its geometry or command-local rollback."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


class SketchCreationError(ToolError):
    """A sketch write could not prove its actual, correctly framed geometry."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


class _SketchContractViolation(ValueError):
    def __init__(self, code: str, message: str, category: str = "adapter_contract") -> None:
        super().__init__(message)
        self.code, self.category = code, category


class SolidWorksGateway(CandidateWorkspaceMixin):
    """The only object allowed to own SolidWorks COM references."""

    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self._sw: Any = None
        self._owns_application = False
        self._com_initialized = False
        self._last_centerline: SketchLineSignature | None = None
        self._pending_revolve: SketchLineSignature | None = None
        self._sketch_session: SketchSessionSignature | None = None
        self._sketch_frame: dict[str, tuple[float, ...]] = dict(PLANE_FRAMES["front"])
        self._last_terminal_signature: PlanarFaceSignature | None = None
        self._last_terminal_frame: dict[str, tuple[float, ...]] | None = None
        self._last_terminal_error = ""
        self._owned_circles: dict[str, dict[str, Any]] = {}
        self._com_identities: list[tuple[Any, str]] = []
        self._allow_start = True  # Set only by the host/worker, never tool arguments.

    def _reset_modeling_context(self, reason: str = "") -> None:
        self._owned_circles.clear()
        self._last_centerline = None
        self._pending_revolve = None
        self._sketch_session = None
        self._last_terminal_signature = None
        self._last_terminal_frame = None
        self._last_terminal_error = reason
        self._sketch_frame = dict(PLANE_FRAMES["front"])

    def connect(self) -> Any:
        if self._sw is not None:
            return self._sw
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            self._com_initialized = True
            try:
                self._sw = win32com.client.GetActiveObject("SldWorks.Application")
                self._owns_application = False
            except Exception as exc:
                if getattr(exc, "hresult", None) != -2147221021:
                    raise ToolError("SolidWorks 会话连接状态无法确认；没有尝试启动新实例。") from exc
                if self._allow_start is not True:
                    raise ToolError("SolidWorks 未运行；当前为仅连接模式，没有启动新实例。") from exc
                self._sw = win32com.client.Dispatch("SldWorks.Application")
                self._owns_application = True
            self._sw.Visible = bool(self.visible)
            return self._sw
        except Exception as exc:
            self.release()
            raise ToolError(f"无法连接 SolidWorks：{exc}") from exc

    def release(self, *, close_owned: bool = False) -> None:
        self.invalidate_candidate_workspace()
        app = self._sw
        if close_owned and self._owns_application and app is not None:
            try:
                if _value(app, "ActiveDoc") is None:
                    _value(app, "ExitApp")
            except Exception:
                # Never force-close an external process. A failed graceful exit
                # leaves SolidWorks running so that user data cannot be lost.
                pass
        self._sw = None
        self._owns_application = False
        self._reset_modeling_context("SolidWorks COM 会话已释放。")
        self._com_identities.clear()
        app = None
        if self._com_initialized:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            finally:
                self._com_initialized = False

    @property
    def document(self) -> Any:
        return _value(self._sw, "ActiveDoc") if self._sw is not None else None

    def probe_document_identity(self) -> dict[str, Any]:
        """Private host probe: no connection, geometry reads, selection, or model tools."""
        empty = {"connected": self._sw is not None, "document_identity": None,
                 "document_title": "", "document_path": "", "document_type": "none"}
        if self._sw is None:
            return empty
        try:
            document = _read_member(self._sw, "ActiveDoc")
            if document is None:
                return empty
            identity = self._com_identity(document)
            title = _read_member(document, "GetTitle")
            path = _read_member(document, "GetPathName")
            code = _read_member(document, "GetType")
            if not isinstance(title, str) or not 1 <= len(title) <= 1024 or "\0" in title:
                raise ValueError("document title is unreadable or invalid")
            if not isinstance(path, str) or len(path) > 32768 or "\0" in path:
                raise ValueError("document path is unreadable or invalid")
            if not isinstance(code, int) or isinstance(code, bool) or code not in {1, 2, 3}:
                raise ValueError("document type is unknown")
            # A document switch during the header reads must not yield evidence
            # that belongs to a formerly active document.
            current = _read_member(self._sw, "ActiveDoc")
            if current is None or self._com_identity(current) != identity:
                raise ValueError("active document changed during identity probe")
            return {"connected": True, "document_identity": identity, "document_title": title,
                    "document_path": path, "document_type": {1: "part", 2: "assembly", 3: "drawing"}[code]}
        except Exception as exc:
            raise ToolError(f"无法可靠读取当前文档身份：{exc}") from exc

    def require_document(self, *, part: bool = False) -> Any:
        document = self.document
        if document is None:
            raise ToolError("当前没有活动 SolidWorks 文档。")
        if part and int(_value(document, "GetType", 0) or 0) != 1:
            raise ToolError("当前活动文档不是零件文档。")
        bound = getattr(self, "_execution_document", None)
        if bound is not None:
            if self._com_identity(document) != self._com_identity(bound):
                from .preconditions import StateChanged
                raise StateChanged("执行期间活动文档已偏离绑定目标，禁止改用新活动文档。", ["document_identity"])
            document = bound
        if getattr(self, "_candidate_write_scope", False):
            self.candidate_write_guard(document)
        return document

    @contextmanager
    def execution_document(self, expected: dict[str, Any]) -> Iterator[None]:
        """Keep one document object for an operation; never redirect via ActiveDoc.

        This is not a lock against external UI or COM writers. Helpers still
        reject an active-document change before using the bound object.
        """
        from .preconditions import StateChanged
        if getattr(self, "_execution_document", None) is not None:
            raise StateChanged("不允许嵌套文档执行作用域。", ["execution_scope"])
        document = self.require_document()
        if self._com_identity(document) != expected.get("document_identity"):
            raise StateChanged("写入目标在文档绑定时发生变化。", ["document_identity"])
        self._execution_document = document
        try:
            yield
        finally:
            self._execution_document = None

    def execute(self, call: ToolCall) -> tuple[str, dict[str, Any]]:
        candidate = getattr(self, "_candidate_active", None)
        read_tools = {"model.observe", "model.inspect", "model.measure_extents", "session.capabilities",
                      "model.references", "sketch.inspect", "feature.inspect", "diagnostics.recent"}
        if candidate is None or call.tool in read_tools:
            return self._execute_unchecked(call)
        allowed = {"selection.plane", "sketch.begin", "sketch.circle", "sketch.rectangle", "sketch.polygon",
                   "sketch.line", "sketch.centerline", "feature.extrude", "feature.revolve"}
        if call.tool not in allowed:
            raise ToolError("试验副本内不允许该文档/会话或引用操作。")
        self.candidate_write_guard(check_stamp=True)
        self._candidate_write_scope = True
        try:
            return self._execute_unchecked(call)
        finally:
            self._candidate_write_scope = False

    def _execute_unchecked(self, call: ToolCall) -> tuple[str, dict[str, Any]]:
        args = call.arguments
        if call.tool == "model.measure_extents":
            return "已查询当前零件的模型坐标系外廓尺寸；请核对 complete 标记。", self._measure_extents()
        if call.tool == "session.capabilities":
            return "已查询运行版本和当前能力线索。", self._inspect_capabilities()
        if call.tool == "model.references":
            return "已查询当前模型的候选参考。", self._inspect_references(int(args.get("limit", 30)))
        if call.tool == "sketch.inspect":
            return "已查询活动草图状态。", self._inspect_sketch()
        if call.tool == "feature.inspect":
            return "已查询指定特征详情。", self._inspect_feature(str(args["name"]), int(args.get("limit", 30)))
        if call.tool == "diagnostics.recent":
            return "最近失败调查需由当前任务提供上下文。", {
                "complete": False, "issues": ["task context is unavailable in the CAD gateway"],
                "failures": [],
            }
        if call.tool == "session.ensure":
            app = self.connect()
            return "SolidWorks 会话可用。", {"revision": str(_value(app, "RevisionNumber", ""))}
        if call.tool == "document.new_part":
            return self._new_part()
        if call.tool == "document.open_readonly":
            return self._open_readonly(str(args["path"]))
        if call.tool == "selection.plane":
            selected = self._select_plane(str(args["plane"]))
            return f"已选择基准面：{selected}", {"selected": selected}
        if call.tool == "selection.capture_reference":
            return self._capture_selection_reference()
        if call.tool == "selection.restore_reference":
            return self._restore_selection_reference(
                str(args["document_path"]), str(args["persistent_ref"])
            )
        if call.tool == "sketch.begin":
            selected = self._begin_sketch(str(args["plane"]))
            return f"已在 {selected} 创建草图。", {"plane": selected}
        if call.tool == "sketch.begin_face":
            self._begin_sketch_on_last_face()
            return "已在上一特征端面创建草图。", {"face": "last_feature_end"}
        if call.tool == "sketch.circle":
            applied = self._circle(
                float(args["x_mm"]), float(args["y_mm"]), float(args["radius_mm"])
            )
            return f"已创建半径 {float(args['radius_mm']):g} mm 的圆。", {
                "applied": applied
            }
        if call.tool == "sketch.set_circle_radius":
            applied = self._set_circle_radius(str(args["entity_ref"]),
                                              float(args["expected_radius_mm"]), float(args["radius_mm"]))
            return "已修改指定圆半径并完成几何回读。", {"applied": applied}
        if call.tool == "sketch.rectangle":
            applied = self._rectangle(
                float(args["center_x_mm"]),
                float(args["center_y_mm"]),
                float(args["width_mm"]),
                float(args["height_mm"]),
            )
            return "已创建中心矩形。", {"applied": applied}
        if call.tool == "sketch.polygon":
            applied = self._polygon(
                float(args["x_mm"]),
                float(args["y_mm"]),
                float(args["radius_mm"]),
                int(args["sides"]),
                float(args["rotation_deg"]),
            )
            return f"已创建正 {int(args['sides'])} 边形。", {"applied": applied}
        if call.tool in {"sketch.line", "sketch.centerline"}:
            applied = self._line(
                float(args["x1_mm"]),
                float(args["y1_mm"]),
                float(args["x2_mm"]),
                float(args["y2_mm"]),
                centerline=call.tool == "sketch.centerline",
            )
            return (
                "已创建旋转中心线。" if call.tool == "sketch.centerline" else "已创建草图直线。",
                {"applied": applied},
            )
        if call.tool == "feature.extrude":
            applied = self._extrude(
                depth_mm=float(args["depth_mm"]),
                operation=str(args["operation"]),
                end_condition=str(args["end_condition"]),
                reverse=bool(args["reverse"]),
                merge=bool(args["merge"]),
            )
            return f"已创建 {float(args['depth_mm']):g} mm 拉伸。", {"applied": applied}
        if call.tool == "feature.revolve":
            applied = self._revolve(
                angle_deg=float(args["angle_deg"]),
                operation=str(args["operation"]),
                reverse=bool(args["reverse"]),
                merge=bool(args["merge"]),
            )
            return f"已创建 {float(args['angle_deg']):g}° 旋转。", {"applied": applied}
        if call.tool == "model.inspect":
            return "已读取当前模型状态。", {"snapshot": self.snapshot().to_dict()}
        if call.tool == "model.observe":
            observation = self._observe_model(include_configuration=bool(args.get("include_configuration", False)))
            return (
                "已完成当前模型只读观察。" if observation["complete"]
                else "模型观察不完整，请检查读取问题后再决定操作。",
                observation,
            )
        if call.tool == "library.inspect_document":
            manifest = self._inspect_document(str(args["path"]))
            return "已读取文档参数。", {"manifest": manifest}
        if call.tool == "library.modify_part_copy":
            manifest = self._modify_part_copy(str(args["path"]), dict(args["changes_mm"]))
            return "已修改副本参数并完成重建验证。", {"manifest": manifest}
        if call.tool == "product_family.generate":
            data = self._generate_product_family(
                str(args["path"]),
                list(args["variants"]),
                use_design_table=bool(args.get("use_design_table", False)),
            )
            return "产品族配置已创建、保存并重开验证。", data
        if call.tool == "assembly.inspect":
            report = self._inspect_assembly(str(args["path"]))
            return "装配体结构、引用和干涉信息已读取。", {"report": report}
        if call.tool == "assembly.create_mate":
            data = self._create_assembly_mate(dict(args))
            return "配合已创建并保留为待人工确认的未保存修改。", data
        if call.tool == "drawing.generate":
            data = self._generate_drawing(dict(args))
            return "工程图已生成、保存并重开验证。", data
        if call.tool == "pmi.read":
            pmi_records = self._read_pmi(str(args["path"]))
            return "已只读读取 DimXpert PMI。", {"pmi": pmi_records}
        if call.tool == "assembly.pack_and_go":
            data = self._pack_and_go(
                str(args["source"]),
                str(args["destination_dir"]),
                include_drawings=bool(args.get("include_drawings", True)),
                include_toolbox=bool(args.get("include_toolbox", True)),
            )
            return "SolidWorks Pack and Go 已完成。", data
        if call.tool == "document.export":
            data = self._export_document(str(args["source"]), str(args["destination"]))
            return "SolidWorks 文档导出已完成。", data
        raise ToolError(f"COM 工作进程不支持工具：{call.tool}")

    def _new_part(self) -> tuple[str, dict[str, Any]]:
        app = self.connect()
        # Ownership cannot be inferred from ActiveDoc after a failed NewPart.
        # Enumerate first, including inactive unsaved user documents.
        try:
            existing = {self._com_identity(item) for item in _items(_read_member(app, "GetDocuments"))}
            previous = _read_member(app, "ActiveDoc")
            if previous is not None:
                existing.add(self._com_identity(previous))
        except Exception as exc:
            raise ToolError("无法读取已打开文档身份，未创建或接管任何零件。") from exc
        document = None
        template = self._part_template(app)
        errors: list[str] = []
        if template:
            try:
                document = app.NewDocument(template, 0, 0.0, 0.0)
            except Exception as exc:
                errors.append(f"NewDocument: {exc}")
        if document is None:
            try:
                document = app.NewPart()
            except Exception as exc:
                errors.append(f"NewPart: {exc}")
        # A concurrently opened user document is not proof of API creation.
        # Require the actual NewDocument/NewPart return value; never adopt ActiveDoc.
        if (document is None or self._com_identity(document) in existing
                or int(_value(document, "GetType", 0) or 0) != 1):
            raise ToolError("SolidWorks 未创建零件文档。" + ("；".join(errors) if errors else ""))
        self._reset_modeling_context()
        self.register_candidate_document(document)
        return "已创建未保存的新零件。", {"title": str(_value(document, "GetTitle", ""))}

    @staticmethod
    def _part_template(app: Any) -> str:
        try:
            path = str(app.GetUserPreferenceStringValue(8) or "")
            if path and Path(path).is_file():
                return path
        except Exception:
            pass
        patterns = [
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\templates\*.prtdot",
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\lang\*\Tutorial\*.prtdot",
        ]
        candidates: list[str] = []
        for pattern in patterns:
            candidates.extend(path for path in glob.glob(pattern) if os.path.isfile(path))
        return sorted(candidates, key=str.casefold, reverse=True)[0] if candidates else ""

    def _open_readonly(self, path: str) -> tuple[str, dict[str, Any]]:
        document, file_path = self._open_readonly_document(path)
        return "已以只读方式打开文档。", {
            "path": file_path,
            "title": str(_value(document, "GetTitle", "")),
        }

    def _open_readonly_document(self, path: str) -> tuple[Any, str]:
        return self._open_document(path, readonly=True)

    def _open_document(self, path: str, *, readonly: bool) -> tuple[Any, str]:
        file_path = str(Path(path).resolve())
        if not Path(file_path).is_file():
            raise ToolError(f"文件不存在：{file_path}")
        doc_type = DOC_TYPES.get(Path(file_path).suffix.casefold())
        if doc_type is None:
            raise ToolError("不支持的 SolidWorks 文件类型。")
        app = self.connect()
        try:
            options = 1 | (2 if readonly else 0)
            result = self._open_doc6(app, file_path, doc_type, options)
        except Exception as exc:
            raise ToolError(f"SolidWorks 打开文件失败：{exc}") from exc
        document = result[0] if isinstance(result, tuple) and result else result
        if not document:
            document = _value(app, "ActiveDoc")
        if not document:
            raise ToolError("SolidWorks 没有返回打开的文档。")
        self._reset_modeling_context("SolidWorks 已打开其他文档，原建模上下文已失效。")
        return document, file_path

    def _export_document(self, source: str, destination: str) -> dict[str, Any]:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve(strict=False)
        if destination_path.exists():
            raise ToolError("导出临时目标已经存在。")
        if not destination_path.parent.is_dir():
            raise ToolError("导出临时目标的上级目录不存在。")
        app = self.connect()
        existing = None
        try:
            existing = app.GetOpenDocumentByName(str(source_path))
        except Exception:
            pass
        opened_here = existing is None
        if opened_here:
            document, _ = self._open_document(str(source_path), readonly=False)
        else:
            document = existing
            if bool(_value(document, "GetSaveFlag", False)):
                raise ToolError("源文档已打开且包含未保存修改；请先保存或关闭后再导出。")
        title = str(_value(document, "GetTitle", "") or "")
        try:
            try:
                activated = self._activate_document(app, title)
                activated_document = activated[0] if isinstance(activated, tuple) else activated
                if activated_document is not None:
                    document = activated_document
            except Exception:
                if self.document is not document:
                    raise ToolError("无法把待导出文档设为活动文档。")
            document.ClearSelection2(True)
            try:
                null_dispatch = self._null_dispatch()
                save_errors = self._int_reference()
                save_warnings = self._int_reference()
                save_result = document.Extension.SaveAs3(
                    str(destination_path),
                    0,
                    1,
                    null_dispatch,
                    null_dispatch,
                    save_errors,
                    save_warnings,
                )
            except Exception as exc:
                raise ToolError(f"SolidWorks SaveAs3 导出失败：{exc}") from exc
            success, error_code, warning_code = self._save_as_status(
                save_result, save_errors, save_warnings
            )
            status_values = [error_code, warning_code]
            errors = [error_code] if error_code != 0 else []
            if not success or errors:
                detail = "、".join(map(str, errors)) or "未知错误"
                raise ToolError(f"SolidWorks 报告导出失败：{detail}。")
            return {"errors": status_values, "destination": str(destination_path)}
        finally:
            if opened_here:
                try:
                    app.CloseDoc(title)
                except Exception:
                    pass

    def _inspect_document(self, path: str) -> dict[str, Any]:
        file_path = str(Path(path).resolve())
        app = self.connect()
        existing = None
        try:
            existing = app.GetOpenDocumentByName(file_path)
        except Exception:
            pass
        opened_here = existing is None
        if opened_here:
            document, _ = self._open_readonly_document(file_path)
        else:
            document = existing
        try:
            configurations = [
                str(item) for item in _items(_value(document, "GetConfigurationNames"))
            ]
            features: list[dict[str, Any]] = []
            dimensions: list[dict[str, Any]] = []
            feature = _value(document, "FirstFeature")
            visited: set[str] = set()
            for _ in range(10000):
                if feature is None:
                    break
                name = str(_value(feature, "Name", ""))
                feature_type = str(_value(feature, "GetTypeName2", ""))
                identity = f"{name}|{feature_type}|{len(features)}"
                if identity in visited:
                    break
                visited.add(identity)
                error_code = 0
                try:
                    error_code = int(feature.GetErrorCode2() or 0)
                except Exception:
                    pass
                features.append(
                    {
                        "index": len(features),
                        "name": name,
                        "type": feature_type,
                        "error_code": error_code,
                    }
                )
                display = _value(feature, "GetFirstDisplayDimension")
                seen_dimensions: set[str] = set()
                for _dimension_index in range(2000):
                    if display is None:
                        break
                    dimension = None
                    try:
                        dimension = display.GetDimension2(0)
                    except Exception:
                        dimension = _value(display, "GetDimension")
                    if dimension is not None:
                        full_name = str(
                            _value(dimension, "FullName", "") or _value(dimension, "Name", "")
                        )
                        parameter_name = self._local_parameter_name(full_name)
                        if parameter_name and parameter_name not in seen_dimensions:
                            seen_dimensions.add(parameter_name)
                            system_value = _value(dimension, "SystemValue", None)
                            dim_type = int(_value(dimension, "GetType", -1) or -1)
                            dimensions.append(
                                {
                                    "name": parameter_name,
                                    "full_name": full_name,
                                    "feature": name,
                                    "parameter_type": dim_type,
                                    "system_value": float(system_value)
                                    if system_value is not None
                                    else None,
                                    "display_value_mm": (
                                        float(system_value) * 1000.0
                                        if system_value is not None and dim_type in {-1, 0, 1}
                                        else None
                                    ),
                                }
                            )
                    next_display = None
                    for method_name in ("GetNext5", "GetNext4", "GetNext3", "GetNext"):
                        try:
                            method = getattr(display, method_name)
                            next_display = method()
                            break
                        except Exception:
                            continue
                    display = next_display
                feature = _value(feature, "GetNextFeature")

            properties: dict[str, str] = {}
            try:
                manager = document.Extension.CustomPropertyManager("")
                names = _items(_value(manager, "GetNames"))
                for name in names:
                    try:
                        result = manager.Get6(str(name), False, "", "", False, False)
                        property_values = _items(result)
                        properties[str(name)] = str(
                            property_values[1] if len(property_values) > 1 else property_values[0]
                        )
                    except Exception:
                        continue
            except Exception:
                pass

            configuration_properties: dict[str, dict[str, str]] = {}
            for configuration in configurations:
                configuration_values: dict[str, str] = {}
                try:
                    manager = document.Extension.CustomPropertyManager(configuration)
                    for property_name in _items(_value(manager, "GetNames")):
                        try:
                            result = manager.Get6(str(property_name), False, "", "", False, False)
                            resolved = _items(result)
                            configuration_values[str(property_name)] = str(
                                resolved[1] if len(resolved) > 1 else resolved[0]
                            )
                        except Exception:
                            continue
                except Exception:
                    pass
                configuration_properties[configuration] = configuration_values

            material = {"name": "", "database": ""}
            for method_name, arguments in (
                ("GetMaterialPropertyName2", ("", "")),
                ("GetMaterialPropertyName", ("",)),
            ):
                try:
                    raw_material = getattr(document, method_name)(*arguments)
                    material_values = [str(value) for value in _items(raw_material) if value]
                    if material_values:
                        material["name"] = material_values[0]
                        if len(material_values) > 1:
                            material["database"] = material_values[1]
                        break
                except Exception:
                    continue

            equations: list[str] = []
            try:
                equation_manager = _value(document, "GetEquationMgr")
                equation_count = int(_value(equation_manager, "GetCount", 0) or 0)
                for equation_index in range(min(equation_count, 10000)):
                    try:
                        equations.append(str(equation_manager.Equation(equation_index)))
                    except Exception:
                        try:
                            equations.append(str(equation_manager.Equation[equation_index]))
                        except Exception:
                            continue
            except Exception:
                pass

            references: list[dict[str, Any]] = []
            if int(_value(document, "GetType", 0) or 0) == 2:
                try:
                    assembly_document = self._cast_document(document, "AssemblyDoc")
                    for component in _items(assembly_document.GetComponents(True)):
                        references.append(
                            {
                                "name": str(_value(component, "Name2", "")),
                                "path": str(_value(component, "GetPathName", "")),
                                "referenced_configuration": str(
                                    _value(component, "ReferencedConfiguration", "")
                                ),
                                "suppressed": bool(_value(component, "IsSuppressed", False)),
                            }
                        )
                except Exception:
                    pass
            drawing: dict[str, Any] = {"sheets": [], "views": []}
            if int(_value(document, "GetType", 0) or 0) == 3:
                drawing_document = self._cast_document(document, "DrawingDoc")
                drawing["sheets"] = [
                    str(name) for name in _items(_value(drawing_document, "GetSheetNames"))
                ]
                view = _value(drawing_document, "GetFirstView")
                for _ in range(10000):
                    if view is None:
                        break
                    drawing["views"].append(
                        {
                            "name": str(_value(view, "GetName2", "")),
                            "type": int(_value(view, "Type", 0) or 0),
                            "referenced_document": str(_value(view, "GetReferencedModelName", "")),
                            "referenced_configuration": str(
                                _value(view, "ReferencedConfiguration", "")
                            ),
                        }
                    )
                    view = _value(view, "GetNextView")
            snapshot = self._snapshot_document(document).to_dict()
            return {
                "schema_version": 2,
                "path": file_path,
                "title": str(_value(document, "GetTitle", "")),
                "document_type": snapshot["document_type"],
                "configurations": configurations,
                "features": features,
                "dimensions": dimensions,
                "custom_properties": properties,
                "configuration_properties": configuration_properties,
                "material": material,
                "equations": equations,
                "references": references,
                "drawing": drawing,
                "geometry": {
                    "body_count": snapshot["body_count"],
                    "volume_m3": snapshot["volume_m3"],
                    "surface_area_m2": snapshot["surface_area_m2"],
                    "mass_kg": snapshot["mass_kg"],
                    "mass_available": snapshot["mass_available"],
                    "feature_count": snapshot["feature_count"],
                },
                "captured_at": utc_now(),
            }
        finally:
            if opened_here:
                try:
                    app.CloseDoc(str(_value(document, "GetTitle", "")))
                except Exception:
                    pass

    def _modify_part_copy(self, path: str, changes_mm: dict[str, Any]) -> dict[str, Any]:
        file_path = str(Path(path).resolve())
        if Path(file_path).suffix.casefold() != ".sldprt" or not Path(file_path).is_file():
            raise ToolError("参数派生只接受现有的 SLDPRT 临时副本。")
        if not changes_mm or len(changes_mm) > 500:
            raise ToolError("参数修改数量必须位于 1 到 500 之间。")
        normalized: dict[str, float] = {}
        for raw_name, raw_value in changes_mm.items():
            name = str(raw_name).strip()
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ToolError(f"参数 {name or '<空名称>'} 不是有效毫米数值。") from exc
            if not name or not math.isfinite(value) or abs(value) > 1_000_000:
                raise ToolError(f"参数 {name or '<空名称>'} 超出安全范围。")
            normalized[name] = value

        app = self.connect()
        try:
            if app.GetOpenDocumentByName(file_path):
                raise ToolError("临时副本已在 SolidWorks 中打开，无法安全派生。")
        except ToolError:
            raise
        except Exception:
            pass
        try:
            opened = self._open_doc6(app, file_path, 1, 1)
        except Exception as exc:
            raise ToolError(f"SolidWorks 无法打开参数派生副本：{exc}") from exc
        document = opened[0] if isinstance(opened, tuple) and opened else opened
        if not document:
            document = _value(app, "ActiveDoc")
        if not document or int(_value(document, "GetType", 0) or 0) != 1:
            raise ToolError("SolidWorks 没有返回可写零件文档。")
        title = str(_value(document, "GetTitle", ""))
        try:
            for name, value_mm in normalized.items():
                try:
                    dimension = self._find_parameter(document, name)
                except Exception as exc:
                    raise ToolError(f"找不到可写尺寸：{name}") from exc
                if dimension is None:
                    raise ToolError(f"找不到可写尺寸：{name}")
                try:
                    dimension.SystemValue = value_mm / 1000.0
                except Exception as exc:
                    raise ToolError(f"尺寸 {name} 不允许写入。") from exc
                actual = _value(dimension, "SystemValue", None)
                if actual is None or abs(float(actual) * 1000.0 - value_mm) > 0.001:
                    raise ToolError(f"尺寸 {name} 写入后读回值不一致。")
            try:
                rebuilt = document.ForceRebuild3(False)
            except Exception as exc:
                raise ToolError(f"参数修改后的模型重建失败：{exc}") from exc
            if rebuilt is False:
                raise ToolError("参数修改后的模型重建失败。")
            try:
                saved = document.Save3(1, 0, 0)
            except Exception as exc:
                raise ToolError(f"参数副本保存失败：{exc}") from exc
            values = _items(saved)
            if values and isinstance(values[0], bool) and not values[0]:
                raise ToolError("SolidWorks 报告参数副本保存失败。")
        finally:
            try:
                app.CloseDoc(title)
            except Exception:
                pass

        manifest = self._inspect_document(file_path)
        observed = {
            str(item.get("name")): item.get("display_value_mm")
            for item in manifest.get("dimensions", [])
        }
        for name, expected in normalized.items():
            actual = observed.get(name)
            if actual is None or abs(float(actual) - expected) > 0.001:
                raise ToolError(f"重开验证未找到参数 {name} 的目标值。")
        return manifest

    def _generate_product_family(
        self,
        path: str,
        variants: list[Any],
        *,
        use_design_table: bool,
    ) -> dict[str, Any]:
        file_path = str(Path(path).resolve())
        if Path(file_path).suffix.casefold() != ".sldprt" or not Path(file_path).is_file():
            raise ToolError("产品族生成只接受现有的 SLDPRT 临时副本。")
        if use_design_table:
            raise ToolError("当前版本使用原生配置生成产品族，尚未启用设计表写入。")
        normalized = self._normalize_family_variants(variants)
        app = self.connect()
        try:
            if app.GetOpenDocumentByName(file_path):
                raise ToolError("产品族临时副本已在 SolidWorks 中打开，无法安全写入。")
        except ToolError:
            raise
        except Exception:
            pass
        try:
            opened = self._open_doc6(app, file_path, 1, 1)
        except Exception as exc:
            raise ToolError(f"SolidWorks 无法打开产品族临时副本：{exc}") from exc
        document = opened[0] if isinstance(opened, tuple) and opened else opened
        if not document:
            document = _value(app, "ActiveDoc")
        if not document or int(_value(document, "GetType", 0) or 0) != 1:
            raise ToolError("SolidWorks 没有返回可写零件文档。")
        title = str(_value(document, "GetTitle", "") or "")
        created: list[str] = []
        try:
            configuration_manager = document.ConfigurationManager
            for variant in normalized:
                name = str(variant["name"])
                configuration = None
                try:
                    configuration = document.GetConfigurationByName(name)
                except Exception:
                    pass
                if configuration is None:
                    try:
                        configuration = configuration_manager.AddConfiguration2(
                            name,
                            "由 SolidWorks 智能助手生成",
                            "",
                            0,
                            "",
                            "",
                            False,
                        )
                    except Exception as exc:
                        raise ToolError(f"创建配置 {name} 失败：{exc}") from exc
                if configuration is None:
                    raise ToolError(f"SolidWorks 未创建配置：{name}")
                try:
                    activated = document.ShowConfiguration2(name)
                except Exception as exc:
                    raise ToolError(f"无法激活配置 {name}：{exc}") from exc
                if activated is False and not self._configuration_is_active(document, name):
                    raise ToolError(f"SolidWorks 拒绝激活配置：{name}")
                for dimension_name, value_mm in variant["dimensions_mm"].items():
                    try:
                        dimension = self._find_parameter(document, dimension_name)
                    except Exception as exc:
                        raise ToolError(f"配置 {name} 找不到尺寸：{dimension_name}") from exc
                    if dimension is None:
                        raise ToolError(f"配置 {name} 找不到尺寸：{dimension_name}")
                    try:
                        status = dimension.SetSystemValue3(value_mm / 1000.0, 1, None)
                    except Exception as exc:
                        raise ToolError(
                            f"配置 {name} 的尺寸 {dimension_name} 不允许写入：{exc}"
                        ) from exc
                    status_values = self._flatten_integers(status)
                    if status_values and status_values[0] != 0:
                        raise ToolError(
                            f"配置 {name} 的尺寸 {dimension_name} 写入失败，状态码 "
                            f"{status_values[0]}。"
                        )
                    actual = _value(dimension, "SystemValue", None)
                    if actual is None or abs(float(actual) * 1000.0 - value_mm) > 0.001:
                        raise ToolError(f"配置 {name} 的尺寸 {dimension_name} 写入后读回值不一致。")
                self._write_configuration_properties(document, name, variant["properties"])
                try:
                    rebuilt = document.ForceRebuild3(False)
                except Exception as exc:
                    raise ToolError(f"配置 {name} 重建失败：{exc}") from exc
                if rebuilt is False:
                    raise ToolError(f"配置 {name} 重建失败。")
                created.append(name)
            try:
                saved = document.Save3(1, 0, 0)
            except Exception as exc:
                raise ToolError(f"产品族副本保存失败：{exc}") from exc
            values = _items(saved)
            if values and isinstance(values[0], bool) and not values[0]:
                raise ToolError("SolidWorks 报告产品族副本保存失败。")
        finally:
            try:
                app.CloseDoc(title)
            except Exception:
                pass

        verification = self._verify_product_family(file_path, normalized)
        manifest = self._inspect_document(file_path)
        return {
            "configurations": created,
            "variants": verification,
            "manifest": manifest,
        }

    @staticmethod
    def _normalize_family_variants(variants: list[Any]) -> list[dict[str, Any]]:
        if not 1 <= len(variants) <= 1000:
            raise ToolError("产品族配置数量必须位于 1 到 1000 之间。")
        normalized: list[dict[str, Any]] = []
        names: set[str] = set()
        dimension_names: set[str] | None = None
        for raw_variant in variants:
            if not isinstance(raw_variant, dict):
                raise ToolError("产品族配置必须是对象。")
            name = str(raw_variant.get("name") or "").strip()
            if not name or any(character in name for character in '\\/:*?"<>|'):
                raise ToolError(f"产品族配置名称无效：{name!r}")
            if name.casefold() in names:
                raise ToolError(f"产品族配置名称重复：{name}")
            names.add(name.casefold())
            raw_dimensions = raw_variant.get("dimensions_mm")
            if not isinstance(raw_dimensions, dict) or not 1 <= len(raw_dimensions) <= 500:
                raise ToolError(f"配置 {name} 必须包含 1 到 500 个尺寸。")
            dimensions: dict[str, float] = {}
            for raw_dimension, raw_value in raw_dimensions.items():
                dimension = str(raw_dimension).strip()
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ToolError(f"配置 {name} 的尺寸 {dimension} 不是有效数值。") from exc
                if not dimension or not math.isfinite(value) or value <= 0 or value > 1_000_000:
                    raise ToolError(f"配置 {name} 的尺寸 {dimension} 超出安全范围。")
                dimensions[dimension] = value
            current_names = set(dimensions)
            if dimension_names is None:
                dimension_names = current_names
            elif dimension_names != current_names:
                raise ToolError("所有产品族配置必须提供相同的尺寸列。")
            raw_properties = raw_variant.get("properties") or {}
            if not isinstance(raw_properties, dict) or len(raw_properties) > 200:
                raise ToolError(f"配置 {name} 的自定义属性格式无效。")
            properties: dict[str, str] = {}
            for raw_property, raw_value in raw_properties.items():
                property_name = str(raw_property).strip()
                property_value = str(raw_value)
                if not property_name or len(property_name) > 255 or len(property_value) > 4000:
                    raise ToolError(f"配置 {name} 包含无效的自定义属性。")
                properties[property_name] = property_value
            normalized.append({"name": name, "dimensions_mm": dimensions, "properties": properties})
        return normalized

    @staticmethod
    def _write_configuration_properties(
        document: Any, configuration: str, properties: dict[str, str]
    ) -> None:
        if not properties:
            return
        try:
            manager = document.Extension.CustomPropertyManager(configuration)
        except Exception as exc:
            raise ToolError(f"无法读取配置 {configuration} 的自定义属性管理器：{exc}") from exc
        for name, value in properties.items():
            try:
                status = manager.Add3(name, 30, value, 2)
            except Exception as exc:
                raise ToolError(f"配置 {configuration} 的属性 {name} 写入失败：{exc}") from exc
            statuses = SolidWorksGateway._flatten_integers(status)
            if statuses and statuses[0] != 0:
                raise ToolError(
                    f"配置 {configuration} 的属性 {name} 写入失败，状态码 {statuses[0]}。"
                )

    def _verify_product_family(
        self, file_path: str, variants: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        app = self.connect()
        document, _ = self._open_readonly_document(file_path)
        title = str(_value(document, "GetTitle", "") or "")
        verified: list[dict[str, Any]] = []
        try:
            available = {
                str(item).casefold(): str(item)
                for item in _items(_value(document, "GetConfigurationNames"))
            }
            for variant in variants:
                name = str(variant["name"])
                if name.casefold() not in available:
                    raise ToolError(f"重开验证未找到产品族配置：{name}")
                selected_name = available[name.casefold()]
                if document.ShowConfiguration2(
                    selected_name
                ) is False and not self._configuration_is_active(document, selected_name):
                    raise ToolError(f"重开验证无法激活产品族配置：{name}")
                observed_dimensions: dict[str, float] = {}
                for dimension_name, expected in variant["dimensions_mm"].items():
                    dimension = self._find_parameter(document, dimension_name)
                    actual = _value(dimension, "SystemValue", None) if dimension else None
                    if actual is None or abs(float(actual) * 1000.0 - expected) > 0.001:
                        raise ToolError(f"重开验证发现配置 {name} 的尺寸 {dimension_name} 不一致。")
                    observed_dimensions[dimension_name] = float(actual) * 1000.0
                self._verify_configuration_properties(document, name, variant["properties"])
                verified.append(
                    {
                        "name": name,
                        "dimensions_mm": observed_dimensions,
                        "properties": dict(variant["properties"]),
                    }
                )
            return verified
        finally:
            try:
                app.CloseDoc(title)
            except Exception:
                pass

    @staticmethod
    def _verify_configuration_properties(
        document: Any, configuration: str, expected: dict[str, str]
    ) -> None:
        if not expected:
            return
        manager = document.Extension.CustomPropertyManager(configuration)
        for name, value in expected.items():
            try:
                result = _items(manager.Get6(name, False, "", "", False, False))
            except Exception as exc:
                raise ToolError(
                    f"重开验证无法读取配置 {configuration} 的属性 {name}：{exc}"
                ) from exc
            text_values = {str(item) for item in result if isinstance(item, str)}
            if value not in text_values:
                raise ToolError(f"重开验证发现配置 {configuration} 的属性 {name} 不一致。")

    @staticmethod
    def _configuration_is_active(document: Any, name: str) -> bool:
        manager = _value(document, "ConfigurationManager")
        active = _value(manager, "ActiveConfiguration")
        active_name = str(_value(active, "Name", "") or "")
        return active_name.casefold() == str(name).casefold()

    @staticmethod
    def _local_parameter_name(name: str) -> str:
        parts = str(name).split("@")
        return "@".join(parts[:2]) if len(parts) >= 3 else str(name)

    @classmethod
    def _find_parameter(cls, document: Any, name: str) -> Any:
        candidates = [str(name)]
        local = cls._local_parameter_name(str(name))
        if local not in candidates:
            candidates.append(local)
        for candidate in candidates:
            try:
                parameter = document.Parameter(candidate)
            except Exception:
                continue
            if parameter is not None:
                return parameter
        return None

    def _inspect_assembly(self, path: str) -> dict[str, Any]:
        source = Path(path).resolve(strict=True)
        if source.suffix.casefold() != ".sldasm":
            raise ToolError("装配体检查只接受 SLDASM。")
        app = self.connect()
        existing = None
        try:
            existing = app.GetOpenDocumentByName(str(source))
        except Exception:
            pass
        opened_here = existing is None
        if opened_here:
            document, _ = self._open_readonly_document(str(source))
        else:
            document = existing
        title = str(_value(document, "GetTitle", "") or "")
        try:
            components: list[dict[str, Any]] = []
            missing: list[str] = []
            assembly = self._cast_document(document, "AssemblyDoc")
            for component in _items(assembly.GetComponents(False)):
                component_path = str(_value(component, "GetPathName", "") or "")
                suppression = int(_value(component, "GetSuppression2", -1) or -1)
                mates = _items(_value(component, "GetMates"))
                if component_path and not Path(component_path).is_file():
                    missing.append(component_path)
                components.append(
                    {
                        "name": str(_value(component, "Name2", "") or ""),
                        "path": component_path,
                        "configuration": str(
                            _value(component, "ReferencedConfiguration", "") or ""
                        ),
                        "suppressed": suppression == 0,
                        "lightweight": suppression in {1, 4},
                        "fixed": bool(_value(component, "IsFixed", False)),
                        "mate_count": len(mates),
                    }
                )
            interferences: list[dict[str, Any]] = []
            interference_error = ""
            try:
                manager = assembly.InterferenceDetectionManager
                manager.TreatCoincidenceAsInterference = False
                manager.TreatSubAssembliesAsComponents = True
                for interference in _items(manager.GetInterferences()):
                    related = _items(_value(interference, "Components"))
                    names = [str(_value(item, "Name2", "") or "") for item in related]
                    interferences.append(
                        {
                            "component_a": names[0] if names else "",
                            "component_b": names[1] if len(names) > 1 else "",
                            "volume_m3": float(_value(interference, "Volume", 0.0) or 0.0),
                        }
                    )
                try:
                    manager.Done()
                except Exception:
                    pass
            except Exception as exc:
                interference_error = str(exc)
            return {
                "path": str(source),
                "components": components,
                "interferences": interferences,
                "missing_references": sorted(set(missing), key=str.casefold),
                "degrees_of_freedom": None,
                "evidence": {
                    "reader": "solidworks-com",
                    "interference_checked": not bool(interference_error),
                    "interference_error": interference_error,
                },
            }
        finally:
            if opened_here:
                try:
                    app.CloseDoc(title)
                except Exception:
                    pass

    def _create_assembly_mate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(arguments.get("assembly_path") or "")).resolve(strict=True)
        if path.suffix.casefold() != ".sldasm":
            raise ToolError("配合目标必须是 SLDASM。")
        mate_types = {
            "coincident": 0,
            "concentric": 1,
            "perpendicular": 2,
            "parallel": 3,
            "distance": 5,
            "angle": 6,
        }
        alignments = {"aligned": 0, "anti_aligned": 1, "closest": 2}
        mate_interfaces = {
            "coincident": "ICoincidentMateFeatureData",
            "concentric": "IConcentricMateFeatureData",
            "perpendicular": "IPerpendicularMateFeatureData",
            "parallel": "IParallelMateFeatureData",
            "distance": "IDistanceMateFeatureData",
            "angle": "IAngleMateFeatureData",
        }
        mate_type = str(arguments.get("mate_type") or "")
        alignment = str(arguments.get("alignment") or "closest")
        if mate_type not in mate_types or alignment not in alignments:
            raise ToolError("配合类型或对齐方式不受支持。")
        first_payload = self._decode_reference(str(arguments.get("first_reference") or ""))
        second_payload = self._decode_reference(str(arguments.get("second_reference") or ""))
        app = self.connect()
        existing = None
        try:
            existing = app.GetOpenDocumentByName(str(path))
        except Exception:
            pass
        opened_here = existing is None
        if opened_here:
            document, _ = self._open_document(str(path), readonly=False)
        else:
            document = existing
        if bool(_value(document, "GetSaveFlag", False)):
            raise ToolError("装配体已有未保存修改，不能自动叠加配合。")
        first = self._object_by_persistent_reference(document.Extension, first_payload)
        second = self._object_by_persistent_reference(document.Extension, second_payload)
        if first is None or second is None:
            if opened_here:
                app.CloseDoc(str(_value(document, "GetTitle", "") or ""))
            raise ToolError("一个或两个配合持久引用已失效。")
        first = self._cast_mate_entity(first)
        second = self._cast_mate_entity(second)
        assembly = self._cast_document(document, "AssemblyDoc")
        try:
            document.ClearSelection2(True)
            preselected = self._select_mate_entity(document, first, False)
            preselected = self._select_mate_entity(document, second, True) and preselected
            preselected = (
                preselected
                and int(document.SelectionManager.GetSelectedObjectCount2(1) or 0) == 2
            )
            raw_data = assembly.CreateMateData(mate_types[mate_type])
            if raw_data is None:
                raise ToolError("SolidWorks 没有返回配合数据对象。")
            data = self._cast_document(raw_data, "IMateFeatureData")
            typed_data = self._cast_document(raw_data, mate_interfaces[mate_type])
            data.TypeName = mate_types[mate_type]
            typed_data.EntitiesToMate = self._mate_entities_array(first, second)
            typed_data.MateAlignment = alignments[alignment]
            if mate_type == "distance":
                typed_data.Distance = float(arguments.get("distance_mm") or 0.0) / 1000.0
            elif mate_type == "angle":
                typed_data.Angle = math.radians(float(arguments.get("angle_degrees") or 0.0))
            feature = assembly.CreateMate(data)
            raw_error_code = _value(data, "ErrorStatus", None)
            error_code = int(raw_error_code) if isinstance(raw_error_code, int) else 0
            if feature is None:
                selected_count = int(
                    document.SelectionManager.GetSelectedObjectCount2(1) or 0
                )
                raise ToolError(
                    "SolidWorks 创建配合失败，"
                    f"错误码 {error_code}，类型 {_value(data, 'TypeName', None)!r}，"
                    f"数据 {type(data).__name__}/{type(typed_data).__name__}，"
                    f"Mark=1 选择数 {selected_count}，"
                    f"实体 {type(first).__name__}/{type(second).__name__}。"
                )
            rebuilt = document.ForceRebuild3(False)
            if rebuilt is False:
                raise ToolError("配合创建后装配体重建失败。")
            return {
                "created": True,
                "error_code": 0,
                "solidworks_status": 1,
                "entities_preselected": preselected,
                "feature_name": str(_value(feature, "Name", "") or ""),
                "document_path": str(path),
                "save_pending": bool(_value(document, "GetSaveFlag", True)),
                "manual_confirmation_required": True,
            }
        except Exception:
            try:
                document.EditUndo2(1)
            except Exception:
                pass
            if opened_here:
                try:
                    app.CloseDoc(str(_value(document, "GetTitle", "") or ""))
                except Exception:
                    pass
            raise

    @staticmethod
    def _decode_reference(value: str) -> bytes:
        try:
            payload = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ToolError("配合持久引用格式无效。") from exc
        if not payload:
            raise ToolError("配合持久引用不能为空。")
        return payload

    def _generate_drawing(self, arguments: dict[str, Any]) -> dict[str, Any]:
        model = Path(str(arguments.get("model_path") or "")).resolve(strict=True)
        template = Path(str(arguments.get("template_path") or "")).resolve(strict=True)
        destination = Path(str(arguments.get("destination") or "")).resolve(strict=False)
        if model.suffix.casefold() not in {".sldprt", ".sldasm"}:
            raise ToolError("工程图源模型必须是 SLDPRT 或 SLDASM。")
        if template.suffix.casefold() not in {".drwdot", ".slddrw"}:
            raise ToolError("工程图模板必须是 DRWDOT 或 SLDDRW。")
        if destination.suffix.casefold() != ".slddrw" or destination.exists():
            raise ToolError("工程图目标必须是尚不存在的 SLDDRW 文件。")
        if not destination.parent.is_dir():
            raise ToolError("工程图目标目录不存在。")
        views = self._normalize_drawing_views(arguments.get("views"))
        include_bom = bool(arguments.get("include_bom"))
        include_balloons = bool(arguments.get("include_balloons"))
        if include_bom and model.suffix.casefold() != ".sldasm":
            raise ToolError("BOM 只适用于装配体工程图。")
        if include_balloons and not include_bom:
            raise ToolError("自动气泡必须与 BOM 一起启用。")
        app = self.connect()
        document = None
        title = ""
        published = False
        model_document = None
        model_title = ""
        model_opened_here = False
        try:
            if template.suffix.casefold() == ".drwdot":
                document = app.NewDocument(str(template), 0, 0.0, 0.0)
            else:
                document, _ = self._open_document(str(template), readonly=False)
            if document is None or int(_value(document, "GetType", 0) or 0) != 3:
                raise ToolError("SolidWorks 没有从模板创建工程图。")
            title = str(_value(document, "GetTitle", "") or "")
            drawing = self._cast_document(document, "DrawingDoc")
            created: dict[str, Any] = {}
            actual_view_names: dict[str, str] = {}
            try:
                model_document = app.GetOpenDocumentByName(str(model))
            except Exception:
                model_document = None
            model_opened_here = model_document is None
            if model_opened_here:
                model_document, _ = self._open_readonly_document(str(model))
            model_title = str(_value(model_document, "GetTitle", "") or "")
            model_view_names = [
                str(item) for item in _items(_value(model_document, "GetModelViewNames"))
            ]
            self._activate_document(app, title)
            active_drawing = self.document
            if (
                active_drawing is None
                or int(_value(active_drawing, "GetType", 0) or 0) != 3
                or str(_value(active_drawing, "GetTitle", "") or "").casefold() != title.casefold()
            ):
                raise ToolError("无法把新工程图重新设为活动文档。")
            orientation_names = self._drawing_orientation_names(model_view_names)
            for index, view_spec in enumerate(views):
                x = float(view_spec["x"] or (0.08 + (index % 3) * 0.10))
                y = float(view_spec["y"] or (0.20 - (index // 3) * 0.08))
                orientation = str(view_spec["orientation"])
                if orientation in orientation_names:
                    view = drawing.CreateDrawViewFromModelView3(
                        str(model), orientation_names[orientation], x, y, 0.0
                    )
                elif orientation == "projected":
                    parent = created.get(str(view_spec["projected_from"]).casefold())
                    if parent is None:
                        raise ToolError(f"投影视图 {view_spec['name']} 的父视图不存在。")
                    document.ClearSelection2(True)
                    selected = document.Extension.SelectByID2(
                        str(_value(parent, "GetName2", "") or ""),
                        "DRAWINGVIEW",
                        0.0,
                        0.0,
                        0.0,
                        False,
                        0,
                        self._null_dispatch(),
                        0,
                    )
                    if not selected:
                        raise ToolError(f"无法选择父视图：{view_spec['projected_from']}")
                    view = drawing.CreateUnfoldedViewAt3(x, y, 0.0, False)
                elif orientation == "section":
                    parent = created.get(str(view_spec["projected_from"]).casefold())
                    if parent is None:
                        raise ToolError(f"剖视图 {view_spec['name']} 的父视图不存在。")
                    view = self._create_section_view(
                        document,
                        drawing,
                        parent,
                        model_document,
                        x=x,
                        y=y,
                        label=chr(ord("A") + index % 26),
                    )
                else:  # pragma: no cover - guarded by _normalize_drawing_views
                    raise ToolError(f"不支持的工程图视图方向：{orientation}")
                if view is None:
                    raise ToolError(f"SolidWorks 未创建工程图视图：{view_spec['name']}")
                requested_name = str(view_spec["name"])
                view.SetName2(requested_name)
                actual_name = str(_value(view, "GetName2", "") or "")
                if not actual_name:
                    raise ToolError(f"工程图视图没有可验证名称：{requested_name}")
                if actual_name.casefold() != requested_name.casefold() and orientation != "section":
                    raise ToolError(f"工程图视图无法命名：{view_spec['name']}")
                view.UseSheetScale = 0
                view.ScaleDecimal = float(view_spec["scale"])
                configuration = str(arguments.get("configuration") or "")
                if configuration:
                    view.ReferencedConfiguration = configuration
                created[str(view_spec["name"]).casefold()] = view
                actual_view_names[str(view_spec["name"]).casefold()] = actual_name
            imported_annotation_count = 0
            if bool(arguments.get("import_model_dimensions")):
                inserted = drawing.InsertModelAnnotations3(
                    0,
                    8 | 32768 | 524288 | 16777216,
                    True,
                    True,
                    False,
                    True,
                )
                imported_annotation_count = len(_items(inserted))
            bom_created = False
            if include_bom:
                template_path = self._bom_template()
                if not template_path:
                    raise ToolError("未找到 SolidWorks BOM 表格模板。")
                base_view = next(iter(created.values()))
                document.ClearSelection2(True)
                base_view_name = str(_value(base_view, "GetName2", "") or "")
                selected = document.Extension.SelectByID2(
                    base_view_name,
                    "DRAWINGVIEW",
                    0.0,
                    0.0,
                    0.0,
                    False,
                    0,
                    self._null_dispatch(),
                    0,
                )
                if not selected:
                    raise ToolError(f"插入 BOM 前无法选择基础视图：{base_view_name}")
                configuration = str(
                    arguments.get("configuration")
                    or _value(base_view, "ReferencedConfiguration", "")
                    or ""
                )
                bom = self._insert_view_bom(
                    base_view,
                    configuration=configuration,
                    template_path=template_path,
                )
                bom_created = _created(bom)
                if not bom_created:
                    raise ToolError("SolidWorks 未创建 BOM。")
                if include_balloons:
                    document.ClearSelection2(True)
                    document.Extension.SelectByID2(
                        str(_value(base_view, "GetName2", "") or ""),
                        "DRAWINGVIEW",
                        0.0,
                        0.0,
                        0.0,
                        False,
                        0,
                        self._null_dispatch(),
                        0,
                    )
                    balloons = drawing.AutoBalloon2(1, False)
                    if balloons is None:
                        raise ToolError("SolidWorks 未创建自动气泡。")
            properties = arguments.get("properties") or {}
            if isinstance(properties, dict):
                self._write_configuration_properties(
                    document, "", {str(name): str(value) for name, value in properties.items()}
                )
            null_dispatch = self._null_dispatch()
            save_errors = self._int_reference()
            save_warnings = self._int_reference()
            result = document.Extension.SaveAs3(
                str(destination),
                0,
                1,
                null_dispatch,
                null_dispatch,
                save_errors,
                save_warnings,
            )
            success, error_code, _warning_code = self._save_as_status(
                result, save_errors, save_warnings
            )
            if not success or error_code != 0 or not destination.is_file():
                raise ToolError(f"SolidWorks 报告工程图保存失败，错误码 {error_code}。")
            published = True
        finally:
            if document is not None:
                try:
                    app.CloseDoc(str(_value(document, "GetTitle", "") or title))
                except Exception:
                    pass
            if model_opened_here and model_title:
                try:
                    app.CloseDoc(model_title)
                except Exception:
                    pass
            if not published:
                destination.unlink(missing_ok=True)
        manifest = self._inspect_document(str(destination))
        observed = {
            str(item.get("name") or "").casefold()
            for item in manifest.get("drawing", {}).get("views", [])
        }
        expected = {
            actual_view_names[str(item["name"]).casefold()].casefold()
            for item in views
        }
        if not expected.issubset(observed):
            destination.unlink(missing_ok=True)
            raise ToolError(
                "工程图重开验证缺少视图："
                f"{sorted(expected - observed)}；实际视图={sorted(observed)}"
            )
        return {
            "destination": str(destination),
            "views": [str(item["name"]) for item in views],
            "view_name_map": dict(actual_view_names),
            "bom_created": include_bom,
            "balloons_created": include_balloons,
            "imported_annotation_count": imported_annotation_count,
            "manifest": manifest,
        }

    @staticmethod
    def _insert_view_bom(
        view: Any,
        *,
        configuration: str,
        template_path: str,
    ) -> Any:
        """Insert a parts-only BOM into a drawing view across SW versions.

        IModelDocExtension.InsertBomTable4 targets part/assembly documents.  A
        drawing BOM belongs to IView, and the newest available IView overload
        must be used.  Parts-only BOMs require an explicit, valid
        configuration; top-level-only BOMs have different configuration rules.
        """

        common = (False, 0.02, 0.28, 1, 1, configuration, template_path)
        for name, tail in (
            ("InsertBomTable6", (False, 0, False, False, False)),
            ("InsertBomTable5", (False, 0, False, False)),
            ("InsertBomTable4", (False, 0, False)),
            ("InsertBomTable2", ()),
        ):
            method = getattr(view, name, None)
            if callable(method):
                return method(*common, *tail)
        raise ToolError("当前 SolidWorks 版本未提供可用的工程图视图 BOM 接口。")

    def _activate_document(self, app: Any, title: str) -> Any:
        """Activate a document with both generated and dynamic COM wrappers."""

        try:
            return app.ActivateDoc3(title, False, 0, 0)
        except Exception as direct_error:
            errors = self._int_reference()
            try:
                return app.ActivateDoc3(title, False, 0, errors)
            except Exception:
                raise direct_error

    def _create_section_view(
        self,
        document: Any,
        drawing: Any,
        parent: Any,
        model_document: Any,
        *,
        x: float,
        y: float,
        label: str,
    ) -> Any:
        parent_name = str(_value(parent, "GetName2", "") or "")
        if not parent_name or not bool(drawing.ActivateView(parent_name)):
            raise ToolError(f"无法激活剖视图父视图：{parent_name or '<unnamed>'}")
        document.ClearSelection2(True)
        center, span = self._section_line_geometry(model_document)
        segment = document.SketchManager.CreateLine(
            center[0],
            center[1] - span,
            0.0,
            center[0],
            center[1] + span,
            0.0,
        )
        if not _created(segment):
            raise ToolError("SolidWorks 未创建剖切线。")
        selected = True
        if hasattr(segment, "Select4"):
            selected = bool(segment.Select4(False, self._null_dispatch()))
        elif hasattr(segment, "Select2"):
            selected = bool(segment.Select2(False, 0))
        if not selected:
            raise ToolError("SolidWorks 无法选择剖切线。")
        # Scale-with-model keeps the section view consistent after rebuild.
        view = drawing.CreateSectionViewAt5(x, y, 0.0, label, 8, None, 0.0)
        if not _created(view):
            raise ToolError("SolidWorks 未创建剖视图。")
        return view

    @staticmethod
    def _section_line_geometry(model_document: Any) -> tuple[tuple[float, float], float]:
        document_type = int(_value(model_document, "GetType", 0) or 0)
        try:
            bounds = _items(
                model_document.GetPartBox(True)
                if document_type == 1
                else model_document.GetBox(0)
                if document_type == 2
                else None
            )
        except Exception:
            bounds = []
        if len(bounds) >= 6:
            values = [float(item) for item in bounds[:6]]
            center = ((values[0] + values[3]) / 2, (values[1] + values[4]) / 2)
            extent = max(
                abs(values[3] - values[0]),
                abs(values[4] - values[1]),
                abs(values[5] - values[2]),
            )
            return center, max(extent * 0.75, 0.001)
        return (0.0, 0.0), 0.1

    @staticmethod
    def _save_as_status(result: Any, errors_ref: Any, warnings_ref: Any) -> tuple[bool, int, int]:
        """Normalize SaveAs3 status from generated and dynamic COM wrappers."""

        values = _items(result)
        success = next(
            (value for value in values if isinstance(value, bool)),
            bool(result),
        )
        returned_codes = [
            int(value) for value in values if isinstance(value, int) and not isinstance(value, bool)
        ]
        error_code = int(getattr(errors_ref, "value", 0) or 0)
        warning_code = int(getattr(warnings_ref, "value", 0) or 0)
        if error_code == 0 and returned_codes:
            error_code = returned_codes[0]
        if warning_code == 0 and len(returned_codes) > 1:
            warning_code = returned_codes[1]
        return bool(success), error_code, warning_code

    @staticmethod
    def _normalize_drawing_views(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not 1 <= len(value) <= 20:
            raise ToolError("工程图必须包含 1 到 20 个视图。")
        result: list[dict[str, Any]] = []
        names: set[str] = set()
        for raw in value:
            if not isinstance(raw, dict):
                raise ToolError("工程图视图必须是对象。")
            name = str(raw.get("name") or "").strip()
            orientation = str(raw.get("orientation") or "")
            scale = float(raw.get("scale") or 1.0)
            if not name or name.casefold() in names:
                raise ToolError("工程图视图名称为空或重复。")
            if orientation not in {"front", "top", "right", "isometric", "projected", "section"}:
                raise ToolError(f"不支持的工程图视图方向：{orientation}")
            if not 0.001 <= scale <= 1000:
                raise ToolError("工程图视图比例超出安全范围。")
            parent = str(raw.get("projected_from") or "").strip()
            if orientation in {"projected", "section"} and not parent:
                raise ToolError(f"视图 {name} 必须指定父视图。")
            names.add(name.casefold())
            result.append(
                {
                    "name": name,
                    "orientation": orientation,
                    "scale": scale,
                    "projected_from": parent,
                    "x": float(raw.get("x") or 0.0),
                    "y": float(raw.get("y") or 0.0),
                }
            )
        for item in result:
            parent = str(item["projected_from"])
            if parent and parent.casefold() not in names:
                raise ToolError(f"视图 {item['name']} 的父视图不存在。")
        return result

    @staticmethod
    def _bom_template() -> str:
        patterns = [
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\lang\*\*.sldbomtbt",
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\lang\*\bom-standard.sldbomtbt",
            r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\*\bom-standard.sldbomtbt",
            r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\*\*.sldbomtbt",
        ]
        candidates: list[str] = []
        for pattern in patterns:
            candidates.extend(path for path in glob.glob(pattern) if os.path.isfile(path))
        if not candidates:
            return ""
        return sorted(
            set(candidates),
            key=lambda value: (
                "bom-standard" not in Path(value).name.casefold(),
                "chinese-simplified" not in value.casefold(),
                value.casefold(),
            ),
        )[0]

    @staticmethod
    def _drawing_orientation_names(values: list[str]) -> dict[str, str]:
        available = {value.casefold(): value for value in values}
        candidates = {
            "front": ("*Front", "*前视"),
            "top": ("*Top", "*上视"),
            "right": ("*Right", "*右视"),
            "isometric": ("*Isometric", "*等轴测"),
        }
        resolved: dict[str, str] = {}
        for orientation, names in candidates.items():
            match = next(
                (available[name.casefold()] for name in names if name.casefold() in available),
                "",
            )
            if not match:
                raise ToolError(f"源模型没有可识别的 {orientation} 标准视图。")
            resolved[orientation] = match
        return resolved

    def _read_pmi(self, path: str) -> list[dict[str, Any]]:
        source = Path(path).resolve(strict=True)
        if source.suffix.casefold() not in {".sldprt", ".sldasm"}:
            raise ToolError("PMI 读取只接受 SLDPRT 或 SLDASM。")
        app = self.connect()
        existing = None
        try:
            existing = app.GetOpenDocumentByName(str(source))
        except Exception:
            pass
        opened_here = existing is None
        if opened_here:
            document, _ = self._open_readonly_document(str(source))
        else:
            document = existing
        title = str(_value(document, "GetTitle", "") or "")
        try:
            active_configuration = _value(
                _value(document, "ConfigurationManager"), "ActiveConfiguration"
            )
            configuration = str(_value(active_configuration, "Name", "") or "")
            manager = document.Extension.DimXpertManager(configuration, True)
            part = _value(manager, "DimXpertPart")
            if part is None:
                return []
            records: list[dict[str, Any]] = []
            for annotation in _items(_value(part, "GetAnnotations")):
                name = str(_value(annotation, "Name", "") or "")
                annotation_type = int(_value(annotation, "Type", 0) or 0)
                display = _value(annotation, "GetDisplayEntity")
                dimension = None
                if display is not None:
                    try:
                        dimension = display.GetDimension2(0)
                    except Exception:
                        dimension = _value(display, "GetDimension")
                value = _value(dimension, "SystemValue", None) if dimension else None
                tolerance_values = _items(
                    _value(dimension, "GetToleranceValues") if dimension else None
                )
                numeric_tolerances = [
                    float(item)
                    for item in tolerance_values
                    if isinstance(item, (int, float)) and not isinstance(item, bool)
                ]
                kind = self._pmi_kind(annotation_type)
                angular = annotation_type in {106, 107, 108, 114, 220, 221}
                factor = 180.0 / math.pi if angular else 1000.0
                records.append(
                    {
                        "name": name,
                        "kind": kind,
                        "value": float(value) * factor if value is not None else None,
                        "unit": "deg" if angular else "mm",
                        "tolerance_lower": (
                            numeric_tolerances[0] * factor if numeric_tolerances else None
                        ),
                        "tolerance_upper": (
                            numeric_tolerances[1] * factor if len(numeric_tolerances) > 1 else None
                        ),
                        "datum_references": [],
                        "annotation_type": annotation_type,
                    }
                )
            return records
        finally:
            if opened_here:
                try:
                    app.CloseDoc(title)
                except Exception:
                    pass

    @staticmethod
    def _pmi_kind(annotation_type: int) -> str:
        if annotation_type == 150:
            return "datum"
        if 201 <= annotation_type <= 219:
            return "geometric_tolerance"
        if annotation_type == 109:
            return "diameter"
        if annotation_type in {106, 107, 108, 114, 220, 221}:
            return "angle"
        if 101 <= annotation_type <= 114:
            return "size"
        return "unknown"

    @staticmethod
    def _cast_document(document: Any, interface: str) -> Any:
        try:
            from win32com.client import CastTo
        except Exception:
            return document
        names = (interface, interface if interface.startswith("I") else "I" + interface)
        for name in names:
            try:
                return CastTo(document, name)
            except Exception:
                continue
        interface_ids = {
            "DrawingDoc": "{83A33D33-27C5-11CE-BFD4-00400513BB57}",
            "IDrawingDoc": "{83A33D33-27C5-11CE-BFD4-00400513BB57}",
            "AssemblyDoc": "{83A33D35-27C5-11CE-BFD4-00400513BB57}",
            "IAssemblyDoc": "{83A33D35-27C5-11CE-BFD4-00400513BB57}",
            "Entity": "{83A33D65-27C5-11CE-BFD4-00400513BB57}",
            "IEntity": "{83A33D65-27C5-11CE-BFD4-00400513BB57}",
            "Face2": "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
            "IFace2": "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
            "Edge": "{83A33D42-27C5-11CE-BFD4-00400513BB57}",
            "IEdge": "{83A33D42-27C5-11CE-BFD4-00400513BB57}",
            "Vertex": "{83A33D63-27C5-11CE-BFD4-00400513BB57}",
            "IVertex": "{83A33D63-27C5-11CE-BFD4-00400513BB57}",
            "Feature": "{83A33D38-27C5-11CE-BFD4-00400513BB57}",
            "IFeature": "{83A33D38-27C5-11CE-BFD4-00400513BB57}",
            "Component2": "{655D6F2A-5441-45D1-8CBA-D35FB26988E4}",
            "IComponent2": "{655D6F2A-5441-45D1-8CBA-D35FB26988E4}",
            "IMateFeatureData": "{6F490CFD-E8F1-49C5-BCDE-1B50EB615FFC}",
            "IAngleMateFeatureData": "{EF4303D7-B4FD-46E7-AD34-D942C420E096}",
            "ICoincidentMateFeatureData": "{503A86BF-A37D-4A55-B8EC-ED180851AE29}",
            "IConcentricMateFeatureData": "{D9E1EBDD-2E8B-424F-9401-0A3C8ED7FAAD}",
            "IDistanceMateFeatureData": "{943EED8D-E13B-4459-BD59-EBCC3F60B915}",
            "IParallelMateFeatureData": "{42A6F332-98CD-4CC0-BF37-135219CB0B5B}",
            "IPerpendicularMateFeatureData": "{A13AA19D-ED6D-4770-9967-D3D60AD44364}",
        }
        interface_id = interface_ids.get(interface)
        if interface_id:
            try:
                import pythoncom
                from win32com.client import Dispatch, gencache

                queried = document._oleobj_.QueryInterface(pythoncom.MakeIID(interface_id))
                wrapper_class = gencache.GetClassForCLSID(interface_id)
                if wrapper_class is not None:
                    return wrapper_class(queried)
                return Dispatch(queried, interface, interface_id)
            except Exception:
                pass
            try:
                from win32com.client import Dispatch

                return Dispatch(document, interface, interface_id)
            except Exception:
                pass
        return document

    @classmethod
    def _cast_mate_entity(cls, value: Any) -> Any:
        for interface in ("Entity", "Face2", "Edge", "Vertex", "Feature", "Component2"):
            candidate = cls._cast_document(value, interface)
            if any(
                hasattr(candidate, method)
                for method in ("Select4", "Select2", "SelectByMark")
            ):
                return candidate
        return value

    def _pack_and_go(
        self,
        source: str,
        destination_dir: str,
        *,
        include_drawings: bool,
        include_toolbox: bool,
    ) -> dict[str, Any]:
        source_path = Path(source).resolve(strict=True)
        destination = Path(destination_dir).resolve(strict=True)
        if source_path.suffix.casefold() != ".sldasm":
            raise ToolError("Pack and Go 只接受 SLDASM 装配体。")
        if not destination.is_dir():
            raise ToolError("Pack and Go 临时目标目录不存在。")
        app = self.connect()
        existing = None
        try:
            existing = app.GetOpenDocumentByName(str(source_path))
        except Exception:
            pass
        opened_here = existing is None
        if opened_here:
            document, _ = self._open_readonly_document(str(source_path))
        else:
            document = existing
        title = str(_value(document, "GetTitle", "") or "")
        try:
            try:
                package = document.Extension.GetPackAndGo()
            except Exception as exc:
                raise ToolError(f"无法创建 Pack and Go 会话：{exc}") from exc
            if package is None:
                raise ToolError("SolidWorks 没有返回 Pack and Go 对象。")
            for property_name, value in (
                ("IncludeDrawings", include_drawings),
                ("IncludeSimulationResults", False),
                ("IncludeToolboxComponents", include_toolbox),
                ("FlattenToSingleFolder", False),
            ):
                try:
                    setattr(package, property_name, bool(value))
                except Exception:
                    continue
            try:
                configured = package.SetSaveToName(True, str(destination))
            except Exception as exc:
                raise ToolError(f"无法设置 Pack and Go 目标目录：{exc}") from exc
            if configured is False:
                raise ToolError("SolidWorks 拒绝 Pack and Go 目标目录。")
            original_paths = self._flatten_strings(_value(package, "GetDocumentNames", []))
            try:
                status_result = document.Extension.SavePackAndGo(package)
            except Exception as exc:
                raise ToolError(f"SolidWorks 保存 Pack and Go 失败：{exc}") from exc
            statuses = self._flatten_integers(status_result)
            failures = [status for status in statuses if status != 0]
            if failures:
                raise ToolError("Pack and Go 返回失败状态：" + ", ".join(map(str, failures)))
            saved_paths = self._flatten_strings(_value(package, "GetDocumentSaveToNames", []))
            return {
                "source_paths": original_paths,
                "saved_paths": saved_paths,
                "statuses": statuses,
            }
        finally:
            if opened_here:
                try:
                    app.CloseDoc(title)
                except Exception:
                    pass

    @staticmethod
    def _flatten_strings(value: Any) -> list[str]:
        output: list[str] = []
        for item in _items(value):
            if isinstance(item, str) and item:
                output.append(item)
            elif isinstance(item, (tuple, list)):
                output.extend(SolidWorksGateway._flatten_strings(item))
        return output

    @staticmethod
    def _flatten_integers(value: Any) -> list[int]:
        output: list[int] = []
        for item in _items(value):
            if isinstance(item, int) and not isinstance(item, bool):
                output.append(item)
            elif isinstance(item, (tuple, list)):
                output.extend(SolidWorksGateway._flatten_integers(item))
        return output

    def _select_plane(self, plane: str) -> str:
        document = self.require_document(part=True)
        if plane not in PLANE_NAMES:
            raise ToolError(f"不支持的基准面：{plane}")
        document.ClearSelection2(True)
        null_dispatch = self._null_dispatch()
        for name in PLANE_NAMES[plane]:
            try:
                if document.Extension.SelectByID2(
                    name, "PLANE", 0.0, 0.0, 0.0, False, 0, null_dispatch, 0
                ):
                    return name
            except Exception:
                continue
        features: list[Any] = []
        feature = _value(document, "FirstFeature")
        for _ in range(5000):
            if feature is None:
                break
            if str(_value(feature, "GetTypeName2", "")) == "RefPlane":
                features.append(feature)
            feature = _value(feature, "GetNextFeature")
        index = PLANE_ORDINAL[plane]
        if index < len(features):
            try:
                selected = bool(features[index].Select2(False, 0))
            except Exception:
                selected = False
            if selected:
                return str(_value(features[index], "Name", plane))
        raise ToolError(f"找不到可选择的 {plane} 基准面。")

    def _capture_selection_reference(self) -> tuple[str, dict[str, Any]]:
        document = self.require_document()
        document_path = str(_value(document, "GetPathName", "") or "")
        if not document_path:
            raise ToolError("命名目标只能保存到已经保存过的 SolidWorks 文档。")
        selection_manager = document.SelectionManager
        selection_count = int(selection_manager.GetSelectedObjectCount2(-1) or 0)
        if selection_count != 1:
            raise ToolError("请在 SolidWorks 中只选择一个面、边、顶点、特征或组件。")
        try:
            selected = selection_manager.GetSelectedObject6(1, -1)
        except Exception as exc:
            raise ToolError(f"无法读取当前选择对象：{exc}") from exc
        if selected is None:
            raise ToolError("SolidWorks 没有返回当前选择对象。")
        try:
            raw_reference = document.Extension.GetPersistReference3(selected)
        except Exception as exc:
            raise ToolError(f"无法为当前对象生成持久引用：{exc}") from exc
        reference_bytes = self._reference_bytes(raw_reference)
        if not reference_bytes:
            raise ToolError("SolidWorks 没有为当前对象生成有效的持久引用。")
        selection_type = int(selection_manager.GetSelectedObjectType3(1, -1) or 0)
        object_name = str(_value(selected, "Name", "") or _value(selected, "Name2", "") or "")
        return "已读取当前选择的持久引用。", {
            "document_path": str(Path(document_path).resolve()),
            "document_title": str(_value(document, "GetTitle", "") or ""),
            "persistent_ref": base64.b64encode(reference_bytes).decode("ascii"),
            "selection_type": selection_type,
            "object_name": object_name,
        }

    def _restore_selection_reference(
        self, document_path: str, persistent_ref: str
    ) -> tuple[str, dict[str, Any]]:
        document = self.require_document()
        active_path = str(_value(document, "GetPathName", "") or "")
        if not active_path:
            raise ToolError("当前活动文档尚未保存，不能恢复持久引用。")
        expected = str(Path(document_path).resolve())
        actual = str(Path(active_path).resolve())
        if expected.casefold() != actual.casefold():
            raise ToolError("命名目标不属于当前活动文档，请先打开对应文件。")
        try:
            payload = base64.b64decode(persistent_ref, validate=True)
        except Exception as exc:
            raise ToolError("保存的持久引用格式无效。") from exc
        if not payload:
            raise ToolError("保存的持久引用为空。")
        selected_object = self._object_by_persistent_reference(document.Extension, payload)
        if selected_object is None:
            raise ToolError("目标对象已不存在或拓扑发生变化，持久引用无法恢复。")
        document.ClearSelection2(True)
        if not self._select_object(selected_object):
            raise ToolError("已找到目标对象，但 SolidWorks 无法选中它。")
        count = int(document.SelectionManager.GetSelectedObjectCount2(-1) or 0)
        if count <= 0:
            raise ToolError("恢复持久引用后没有检测到选择对象。")
        return "已恢复命名目标选择。", {"selection_count": count}

    @staticmethod
    def _reference_bytes(value: Any) -> bytes:
        if hasattr(value, "value"):
            value = value.value
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, (tuple, list)):
            output = bytearray()
            for item in value:
                if isinstance(item, (bytes, bytearray)):
                    output.extend(item)
                elif isinstance(item, memoryview):
                    output.extend(item.tobytes())
                elif isinstance(item, int) and 0 <= item <= 255:
                    output.append(item)
            return bytes(output)
        return b""

    @staticmethod
    def _object_by_persistent_reference(extension: Any, payload: bytes) -> Any:
        candidates: list[Any] = [memoryview(payload), payload, tuple(payload), list(payload)]
        try:
            import pythoncom
            from win32com.client import VARIANT

            candidates.insert(
                0, VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, list(payload))
            )
        except Exception:
            pass
        for candidate in candidates:
            attempts: list[tuple[Any, ...]] = [(candidate,), (candidate, 0)]
            try:
                import pythoncom
                from win32com.client import VARIANT

                attempts.append(
                    (candidate, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
                )
            except Exception:
                pass
            for arguments in attempts:
                try:
                    result = extension.GetObjectByPersistReference3(*arguments)
                except Exception:
                    continue
                values = _items(result)
                for value in values:
                    if value is not None and (
                        hasattr(value, "_oleobj_")
                        or any(
                            hasattr(value, method)
                            for method in ("Select4", "Select2", "SelectByMark")
                        )
                    ):
                        return value
        return None

    def _select_object(self, value: Any) -> bool:
        null_dispatch = None
        try:
            null_dispatch = self._null_dispatch()
        except Exception:
            pass
        attempts = (
            ("Select4", (False, null_dispatch)),
            ("Select4", (False, null_dispatch, False)),
            ("Select2", (False, 0)),
            ("Select", (False,)),
        )
        for method_name, arguments in attempts:
            try:
                method = getattr(value, method_name)
                if bool(method(*arguments)):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _null_dispatch() -> Any:
        import pythoncom
        from win32com.client import VARIANT

        return VARIANT(pythoncom.VT_DISPATCH, None)

    @staticmethod
    def _int_reference() -> Any:
        import pythoncom
        from win32com.client import VARIANT

        return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

    @staticmethod
    def _bool_reference() -> Any:
        import pythoncom
        from win32com.client import VARIANT

        return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)

    def _open_doc6(self, app: Any, path: str, document_type: int, options: int) -> Any:
        try:
            return app.OpenDoc6(path, document_type, options, "", 0, 0)
        except Exception as original:
            errors = self._int_reference()
            warnings = self._int_reference()
            try:
                return app.OpenDoc6(
                    path,
                    document_type,
                    options,
                    "",
                    errors,
                    warnings,
                )
            except Exception:
                raise original

    def _begin_sketch(self, plane: str) -> str:
        document = self.require_document(part=True)
        if _value(document.SketchManager, "ActiveSketch") is not None:
            raise ToolError("已有活动草图，请先完成当前草图。")
        selected = self._select_plane(plane)
        document.SketchManager.InsertSketch(True)
        active_sketch = _value(document.SketchManager, "ActiveSketch")
        if active_sketch is None:
            raise ToolError("SolidWorks 未进入草图编辑状态。")
        self._last_centerline = None
        self._pending_revolve = None
        self._sketch_frame = dict(PLANE_FRAMES[plane])
        self._last_terminal_signature = None
        self._last_terminal_frame = None
        self._last_terminal_error = "已改在标准基准面创建草图。"
        self._bind_sketch_session(document, active_sketch)
        return selected

    def _begin_sketch_on_last_face(self) -> None:
        document = self.require_document(part=True)
        manager = document.SketchManager
        if _value(manager, "ActiveSketch") is not None:
            raise ToolError("已有活动草图，请先完成当前草图。")
        if self._last_terminal_signature is None or self._last_terminal_frame is None:
            detail = f"：{self._last_terminal_error}" if self._last_terminal_error else "。"
            raise ToolError(f"没有可唯一解析的新草图端面{detail}")
        rebuilt = document.ForceRebuild3(False)
        if rebuilt is False:
            raise ToolError("解析端面前 SolidWorks 重建失败。")
        try:
            resolved = resolve_planar_face(
                document,
                self._last_terminal_signature,
                lambda payload: self._object_by_persistent_reference(document.Extension, payload),
            )
        except TopologyResolutionError as exc:
            raise ToolError(f"上一特征端面解析失败：{exc}") from exc
        document.ClearSelection2(True)
        if not self._select_object(resolved.face):
            raise ToolError("SolidWorks 无法选中上一特征的平面端面。")
        selected_count = int(document.SelectionManager.GetSelectedObjectCount2(-1) or 0)
        if selected_count != 1:
            document.ClearSelection2(True)
            raise ToolError(f"端面选择数量应为 1，实际为 {selected_count}。")
        manager.InsertSketch(True)
        active_sketch = _value(manager, "ActiveSketch")
        if active_sketch is None:
            raise ToolError("SolidWorks 未在上一特征端面进入草图编辑状态。")
        self._sketch_frame = dict(self._last_terminal_frame)
        self._last_terminal_signature = None
        self._last_terminal_frame = None
        self._last_terminal_error = "端面目标已用于创建草图。"
        self._last_centerline = None
        self._pending_revolve = None
        self._bind_sketch_session(document, active_sketch)

    def _bind_sketch_session(self, document: Any, active_sketch: Any) -> None:
        self._owned_circles.clear()
        try:
            self._sketch_session = capture_sketch_session_signature(
                document, active_sketch
            )
        except TopologyResolutionError as exc:
            self._sketch_session = None
            raise ToolError(f"无法建立草图会话边界：{exc}") from exc

    def _require_bound_active_sketch(self, document: Any) -> Any:
        active_sketch = _value(document.SketchManager, "ActiveSketch")
        if active_sketch is None:
            raise ToolError("当前没有活动草图。")
        if self._sketch_session is None:
            raise ToolError(
                "当前草图不是由本次建模会话创建或恢复的草图，拒绝依赖残留选择继续写入。"
            )
        try:
            validate_sketch_session(document, active_sketch, self._sketch_session)
        except TopologyResolutionError as exc:
            self._reset_modeling_context(str(exc))
            raise ToolError(f"草图会话已经失效：{exc}") from exc
        return active_sketch

    def _sketch_point(self, x_mm: float, y_mm: float) -> tuple[float, float, float]:
        """Public sketch-frame millimetres to MODEL metres, not native sketch XY."""
        origin = self._sketch_frame.get("origin", (0.0, 0.0, 0.0))
        u = self._sketch_frame.get("u", (1.0, 0.0, 0.0))
        v = self._sketch_frame.get("v", (0.0, 1.0, 0.0))
        x_m = x_mm / 1000.0
        y_m = y_mm / 1000.0
        return (
            float(origin[0]) + x_m * float(u[0]) + y_m * float(v[0]),
            float(origin[1]) + x_m * float(u[1]) + y_m * float(v[1]),
            float(origin[2]) + x_m * float(u[2]) + y_m * float(v[2]),
        )

    def _frame_evidence(self) -> dict[str, list[float]]:
        return {
            name: [float(value) for value in self._sketch_frame.get(name, default)]
            for name, default in (
                ("origin", (0.0, 0.0, 0.0)),
                ("u", (1.0, 0.0, 0.0)),
                ("v", (0.0, 1.0, 0.0)),
                ("normal", (0.0, 0.0, 1.0)),
            )
        }

    def _create_verified_sketch_geometry(
        self, tool: str, model_points: list[Any], maximum_added: int,
        create: Any, verify: Any, finalize: Any = None,
    ) -> dict[str, Any]:
        """One bounded write; native sketch readback, never requested-coordinate echo.

        ModelToSketchTransform is authoritative even on translated/rotated faces.
        Explicit MathPoint dispatch is shared with the verified polygon path.
        Failed or ambiguous writes are NOT replayed/deleted here: the outer
        candidate transaction must independently restore the whole document.
        """
        evidence: dict[str, Any] = {
            "kind": "sketch_creation", "tool": tool, "mutation_attempted": False,
            "geometry_verified": False, "rollback_verified": False, "requires_review": False,
            "coordinate_space": {"requested_points_m": "model", "api_points_m": "sketch",
                                 "actual_geometry": "sketch"},
            "requested_points_m": [list(point) for point in model_points],
        }
        try:
            document = self.require_document(part=True)
            sketch = self._require_bound_active_sketch(document)
            manager = document.SketchManager
            before, prior_entities = self._capture_edit_geometry(document, sketch)
            if len(before["segments"]) + maximum_added > 128:
                raise ValueError("creation would exceed the fully readable sketch segment limit")
            transform = _read_member(sketch, "ModelToSketchTransform")
            transform_data = self._strict_vector(_read_member(transform, "ArrayData"), 16)
            # A CAD sketch uses metres with a rigid coordinate transform. A
            # malformed scale must not silently change a circle's world radius.
            rows = [transform_data[index:index + 3] for index in (0, 3, 6)]
            if (not math.isclose(transform_data[12], 1.0, rel_tol=1e-9, abs_tol=1e-9)
                    or any(not math.isclose(sum(a * b for a, b in zip(first, second, strict=True)),
                                           float(i == j), rel_tol=1e-9, abs_tol=1e-9)
                           for i, first in enumerate(rows) for j, second in enumerate(rows))):
                raise ValueError("active sketch transform is not a rigid metre coordinate frame")
            inverse = _read_member(transform, "Inverse")
            local_points = [self._polygon_transform_point(point, transform) for point in model_points]
            for model_point, local_point in zip(model_points, local_points, strict=True):
                if abs(local_point[2]) > 1e-8 or not self._polygon_points_close(
                    self._polygon_transform_point(local_point, inverse), model_point
                ):
                    raise ValueError("requested geometry is not in the active 2D sketch plane")
            evidence["api_points_m"] = local_points
            add_to_db = _read_member(manager, "AddToDB")
            display_when_added = _read_member(manager, "DisplayWhenAdded")
            if any(value not in {True, False, 0, 1, -1} for value in (add_to_db, display_when_added)):
                raise ValueError("sketch manager flags are unknown")

            def capture() -> tuple[dict[str, Any], dict[str, Any]]:
                # Inspect boundaries before session validation clears a stale
                # binding, so configuration/sketch switches retain their safety category.
                active_document = _read_member(self._sw, "ActiveDoc")
                if active_document is None or self._com_identity(active_document) != before["document_identity"]:
                    raise _SketchContractViolation("document_changed", "active document changed during sketch creation", "safety")
                active_configuration = _read_member(
                    _read_member(_read_member(active_document, "ConfigurationManager"), "ActiveConfiguration"), "Name")
                if active_configuration != before["configuration"]:
                    raise _SketchContractViolation("configuration_changed", "active configuration changed during sketch creation", "safety")
                active_sketch = _read_member(_read_member(active_document, "SketchManager"), "ActiveSketch")
                if active_sketch is None or self._com_identity(active_sketch) != self._com_identity(sketch):
                    raise _SketchContractViolation("sketch_changed", "active sketch changed during sketch creation", "safety")
                current_document = self.require_document(part=True)
                if self._com_identity(current_document) != before["document_identity"]:
                    raise _SketchContractViolation("document_changed", "active document changed during sketch creation", "safety")
                current_sketch = self._require_bound_active_sketch(current_document)
                if self._com_identity(current_sketch) != self._com_identity(sketch):
                    raise _SketchContractViolation("sketch_changed", "active sketch changed during sketch creation", "safety")
                current_transform = self._strict_vector(
                    _read_member(_read_member(current_sketch, "ModelToSketchTransform"), "ArrayData"), 16)
                if current_transform != transform_data:
                    raise _SketchContractViolation("transform_changed", "active sketch transform changed during creation", "safety")
                current, entities = self._capture_edit_geometry(document, sketch)
                if any(current[key] != before[key] for key in (
                    "document_identity", "sketch_ref", "sketch_name", "configuration"
                )):
                    raise _SketchContractViolation("configuration_changed", "document or sketch boundary changed during creation", "safety")
                return current, entities

            # Avoid grid snapping / automatic relations from the FIRST call.
            # False, exceptions, or missing readback do not authorize retries.
            creation_error: Exception | None = None
            created: list[Any] = []
            flags_restored = False
            try:
                evidence["requires_review"] = True
                manager.AddToDB = True
                manager.DisplayWhenAdded = False
                if (_read_member(manager, "AddToDB") not in {True, 1, -1}
                        or _read_member(manager, "DisplayWhenAdded") not in {False, 0}):
                    raise ValueError("exact geometry creation flags were not accepted")
                evidence["mutation_attempted"] = True
                created = _items(create(manager, local_points))
            except Exception as exc:
                creation_error = exc
            finally:
                try:
                    manager.AddToDB = bool(add_to_db)
                    manager.DisplayWhenAdded = bool(display_when_added)
                    restored_add = _read_member(manager, "AddToDB")
                    restored_display = _read_member(manager, "DisplayWhenAdded")
                    flags_restored = (
                        restored_add in {True, False, 0, 1, -1}
                        and restored_display in {True, False, 0, 1, -1}
                        and bool(restored_add) == bool(add_to_db)
                        and bool(restored_display) == bool(display_when_added))
                except Exception:
                    pass
                evidence["sketch_manager_flags_restored"] = flags_restored

            # Even when COM returned an error, preserve readable actual geometry
            # as evidence. This does not convert an ambiguous write into success.
            try:
                document.GraphicsRedraw2()
                after, entities = capture()
                old = {item["entity_ref"]: item for item in before["segments"]}
                current = {item["entity_ref"]: item for item in after["segments"]}
                added = [item for key, item in current.items() if key not in old]
                evidence["actual_geometry"] = [
                    {key: value for key, value in item.items() if key not in {"persist_reference", "sketch_ref"}}
                    for item in added[:maximum_added + 1]
                ]
                if any(current.get(key) != item for key, item in old.items()):
                    raise _SketchContractViolation("existing_geometry_changed", "pre-existing sketch geometry changed", "safety")
            except Exception as exc:
                if isinstance(exc, _SketchContractViolation):
                    raise
                if creation_error is not None:
                    raise ValueError(f"{creation_error}; actual geometry readback failed: {exc}") from exc
                raise
            if creation_error is not None:
                raise creation_error
            if not flags_restored:
                raise ValueError("sketch manager flags could not be restored")
            if not added and not any(_created(item) for item in created):
                raise _SketchContractViolation("creation_no_entities", "SolidWorks 未创建草图实体，已独立确认草图未变。", "planning")
            if not added or len(added) > maximum_added or not created or not all(_created(item) for item in created):
                raise ValueError("SolidWorks 未创建可完整验证的草图实体。")
            returned_ids = [self._com_identity(item) for item in created]
            actual_ids = {self._com_identity(entities[item["entity_ref"]]) for item in added}
            if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != actual_ids:
                raise ValueError("returned sketch entities do not match the actual new geometry")
            result = verify(added, inverse)
            if finalize is not None:
                result.update(finalize(document, sketch, created, prior_entities, added, result))
            return {
                **evidence, **result, "frame": self._frame_evidence(), "creation_verified": True,
                "geometry_verified": True, "requires_review": False, "sketch_ref": after["sketch_ref"],
                "geometry_source": "actual ISketchSegment geometry and ISketch.ModelToSketchTransform",
            }
        except Exception as exc:
            evidence["failure_category"] = getattr(exc, "category", "adapter_contract")
            evidence["contract_failure_code"] = getattr(exc, "code", "unverified_sketch_contract")
            evidence["verification_error"] = str(exc)[:700]
            raise SketchCreationError(f"{tool} 未通过实际草图几何验证：{exc}", evidence) from exc

    def _match_created_edges(self, edges: list[dict[str, Any]], points: list[Any],
                             inverse: Any, *, closed: bool, construction: bool) -> list[dict[str, Any]]:
        expected = list(zip(points, points[1:] + points[:1], strict=True)) if closed else [(points[0], points[1])]
        if len(edges) != len(expected):
            raise ValueError("actual sketch edge count does not match the request")
        actual: dict[int, dict[str, Any]] = {}
        for edge in edges:
            if edge["type"] != 0 or edge["construction"] is not construction:
                raise ValueError("unexpected sketch edge type or construction state")
            if any(abs(edge[key][2]) > 1e-8 for key in ("start_sketch_m", "end_sketch_m")):
                raise ValueError("actual line is outside the 2D sketch plane")
            start = self._polygon_transform_point(edge["start_sketch_m"], inverse)
            end = self._polygon_transform_point(edge["end_sketch_m"], inverse)
            matches = []
            for index, (wanted_start, wanted_end) in enumerate(expected):
                if index in actual:
                    continue
                if self._polygon_points_close(start, wanted_start) and self._polygon_points_close(end, wanted_end):
                    matches.append((index, start, end))
                elif self._polygon_points_close(end, wanted_start) and self._polygon_points_close(start, wanted_end):
                    matches.append((index, end, start))
            if len(matches) != 1:
                raise ValueError("actual line endpoints do not match the requested model-space points")
            index, start, end = matches[0]
            actual[index] = {"entity_ref": edge["entity_ref"], "start_m": start, "end_m": end}
        return [actual[index] for index in range(len(expected))]

    def _circle(self, x_mm: float, y_mm: float, radius_mm: float) -> dict[str, Any]:
        center = self._sketch_point(x_mm, y_mm)

        def verify(added: list[dict[str, Any]], inverse: Any) -> dict[str, Any]:
            if len(added) != 1:
                raise ValueError("circle did not create exactly one new segment")
            circle = added[0]
            actual_center = self._polygon_transform_point(circle.get("center_sketch_m"), inverse)
            if (circle.get("is_circle") is not True or circle["construction"]
                    or not self._polygon_points_close(actual_center, center)
                    or not math.isclose(circle["radius_mm"], radius_mm, rel_tol=1e-9, abs_tol=1e-6)
                    or abs(circle["center_sketch_m"][2]) > 1e-8):
                raise ValueError("actual circle center/radius/plane does not match the request")
            return {"center_m": actual_center, "radius_m": circle["radius_mm"] / 1000.0}

        def register(document: Any, sketch: Any, created: list[Any], prior: dict[str, Any],
                     added: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
            registration = self._register_created_circle(
                document, sketch, created[0], {self._com_identity(item) for item in prior.values()},
                center, radius_mm)
            if registration.get("creation_verified") is not True:
                raise ValueError("circle provenance verification failed: "
                                 + registration.get("repair_unavailable", "unknown reason"))
            return registration

        return self._create_verified_sketch_geometry(
            "sketch.circle", [center], 1,
            lambda manager, points: [manager.CreateCircleByRadius(*points[0], radius_mm / 1000.0)],
            verify, register)

    def _com_identity(self, entity: Any) -> str:
        import pythoncom

        # PyIUnknown is unhashable, and separate wrappers may represent the same
        # COM object. Equality on canonical IUnknown provides identity. Retaining
        # the strong references also prevents pointer reuse while tokens are live.
        unknown = entity._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        if unknown is None:
            raise ValueError("COM identity is unavailable")
        for known, token in self._com_identities:
            if known == unknown:
                return token
        if len(self._com_identities) >= 4096:
            raise ValueError("COM identity registry limit reached; reconnect at a safe checkpoint")
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._com_identities.append((unknown, token))
        return token

    @staticmethod
    def _geometry_digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode("utf-8")).hexdigest()

    def _owning_sketch_feature(self, document: Any, sketch: Any) -> Any:
        """ISketch has no GetFeature: resolve a real feature and prove its sketch identity."""
        identity = self._com_identity(sketch)

        def matches(feature: Any) -> bool:
            if feature is None or _read_member(feature, "GetTypeName2") not in {"ProfileFeature", "3DProfileFeature"}:
                return False
            specific = _read_member(feature, "GetSpecificFeature2")
            return specific is not None and self._com_identity(specific) == identity

        name = _value(sketch, "Name", "")
        if isinstance(name, str) and 1 <= len(name) <= 250:
            try:
                candidate = _read_member(document, "FeatureByName", name)
                if _read_member(candidate, "Name") == name and matches(candidate):
                    return candidate
            except Exception:
                pass
        feature = _read_member(document, "FirstFeature")
        seen: set[str] = set()
        for _ in range(3000):
            if feature is None:
                break
            token = self._com_identity(feature)
            if token in seen:
                raise ValueError("cyclic feature traversal while identifying the active sketch")
            seen.add(token)
            if matches(feature):
                return feature
            feature = _read_member(feature, "GetNextFeature")
        raise ValueError("active sketch cannot be matched to a unique owning feature")

    def _segment_geometry(self, segment: Any) -> dict[str, Any]:
        kind = _read_member(segment, "GetType")
        construction = _read_member(segment, "ConstructionGeometry")
        if isinstance(kind, bool) or kind not in {0, 1} or construction not in {True, False, 0, 1, -1}:
            raise ValueError("only readable line and arc geometry supports bounded circle repair")
        details: dict[str, Any] = {"type": kind, "construction": bool(construction)}
        specific = self._cast_document(segment, "SketchLine" if kind == 0 else "SketchArc")

        def point(member: str) -> list[float]:
            value = _read_member(specific, member)
            return self._strict_vector([_read_member(value, axis) for axis in ("X", "Y", "Z")], 3)

        if kind == 1:
            circle = _read_member(specific, "IsCircle")
            if circle not in {0, 1}:
                raise ValueError("arc completeness is unknown")
            radius = float(_read_member(specific, "GetRadius")) * 1000
            if not math.isfinite(radius) or radius <= 0:
                raise ValueError("circle radius is unknown")
            details.update(is_circle=circle == 1, center_sketch_m=point("GetCenterPoint2"), radius_mm=radius,
                           normal_sketch=self._strict_vector(_read_member(specific, "GetNormalVector"), 3))
        if kind == 0 or not details.get("is_circle"):
            details.update(start_sketch_m=point("GetStartPoint2"), end_sketch_m=point("GetEndPoint2"))
        return details

    def _capture_edit_geometry(self, document: Any, sketch: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Strict bounded identity + geometry, independent of enumeration order."""
        if _read_member(sketch, "Is3D") not in {False, 0}:
            raise ValueError("circle repair is restricted to a known 2D sketch")
        document_id = self._com_identity(document)
        sketch_id = self._com_identity(sketch)
        feature = self._owning_sketch_feature(document, sketch)
        name = _read_member(feature, "Name")
        if not isinstance(name, str) or not 1 <= len(name) <= 250:
            raise ValueError("sketch identity is unknown")
        configuration = _read_member(_read_member(_read_member(document, "ConfigurationManager"),
                                                   "ActiveConfiguration"), "Name")
        if not isinstance(configuration, str) or not configuration:
            raise ValueError("active configuration is unknown")
        probe = _ReadProbe()
        reference = self._read_persistent_reference(document, feature, probe)
        if reference is None:
            raise ValueError("sketch persistent reference is unavailable")
        document_token = document_id
        sketch_ref = self._geometry_digest([document_token, sketch_id, reference, configuration])
        raw = _read_member(sketch, "GetSketchSegments")
        if raw is not None and not isinstance(raw, (list, tuple)):
            raise ValueError("sketch segment collection is unknown")
        segments = _items(raw)
        if len(segments) > 128:
            raise ValueError("circle repair requires at most 128 fully readable sketch segments")
        output: list[dict[str, Any]] = []
        entities: dict[str, Any] = {}
        identifiers: set[tuple[int, ...]] = set()
        for segment in segments:
            entry = self._segment_geometry(segment)
            pair = _read_member(segment, "GetID")
            if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                    or any(isinstance(item, bool) or not isinstance(item, int) for item in pair)):
                raise ValueError("sketch segment id is unavailable")
            typed_id = (entry["type"], *pair)
            if typed_id in identifiers:
                raise ValueError("ambiguous or duplicate sketch segment id")
            identifiers.add(typed_id)
            persistent = self._read_persistent_reference(document, segment, probe)
            if persistent is None:
                raise ValueError("sketch segment persistent reference is unavailable")
            entity_ref = self._geometry_digest([sketch_ref, typed_id, persistent])
            entry.update(entity_ref=entity_ref, entity_id=list(pair), persist_reference=persistent,
                         sketch_ref=sketch_ref)
            entities[entity_ref] = segment
            output.append(entry)
        output.sort(key=lambda item: item["entity_ref"])
        data = {"document_identity": document_token, "sketch_ref": sketch_ref, "sketch_name": name,
                "configuration": configuration, "segments": output, "geometry_complete": True}
        # Oversize evidence must block the write, not be truncated afterward.
        if bound_investigation_result(data).get("output_error"):
            raise ValueError("full sketch repair evidence exceeds 60 KB")
        return data, entities

    def _register_created_circle(self, document: Any, sketch: Any, entity: Any, prior_ids: Any,
                                 center_m: tuple[float, float, float], radius_mm: float) -> dict[str, Any]:
        try:
            identity = self._com_identity(entity)
            if prior_ids is None or identity in prior_ids:
                raise ValueError("circle creation identity is not a new entity")
            geometry, entities = self._capture_edit_geometry(document, sketch)
            matching = [key for key, item in entities.items() if self._com_identity(item) == identity]
            if len(matching) != 1 or len(entities) != len(prior_ids) + 1:
                raise ValueError("circle creation did not yield exactly one new entity")
            entry = next(item for item in geometry["segments"] if item["entity_ref"] == matching[0])
            utility = _read_member(self._sw, "GetMathUtility")
            import pythoncom
            from win32com.client import VARIANT

            # MathUtility requires a double SAFEARRAY. Late-bound getters can
            # eagerly return a default MathPoint and discard method arguments;
            # invoke these two fixed methods explicitly, then verify readback.
            coordinates = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_m))
            point = self._invoke_math_point_method(utility, "CreatePoint", coordinates)
            actual_input = self._strict_vector(_read_member(point, "ArrayData"), 3)
            if any(not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
                   for actual, expected in zip(actual_input, center_m, strict=True)):
                raise ValueError("math point input does not match the requested model-space center")
            transformed = self._invoke_math_point_method(
                point, "MultiplyTransform", _read_member(sketch, "ModelToSketchTransform"))
            expected_center = self._strict_vector(_read_member(transformed, "ArrayData"), 3)
            if (entry.get("is_circle") is not True or entry["construction"]
                    or not math.isclose(entry["radius_mm"], radius_mm, rel_tol=1e-9, abs_tol=1e-6)
                    or any(not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
                           for actual, expected in zip(entry["center_sketch_m"], expected_center, strict=True))):
                raise ValueError("new circle geometry does not match the requested center and radius")
            self._owned_circles[matching[0]] = {
                "document": self._com_identity(document), "sketch": self._com_identity(sketch),
                "entity": identity, "geometry": entry,
            }
            return {"creation_verified": True, "entity_ref": matching[0], "sketch_ref": geometry["sketch_ref"],
                    "center_sketch_m": entry["center_sketch_m"], "radius_mm": entry["radius_mm"]}
        except Exception as exc:
            # Uncertain entities gain no edit authority. The creation caller
            # also fails closed when this registration cannot be verified.
            return {"creation_verified": False, "repair_unavailable": str(exc)[:300]}

    @staticmethod
    def _invoke_math_point_method(obj: Any, name: str, argument: Any) -> Any:
        """Invoke only fixed math APIs without eager late-bound property reads."""
        if name not in {"CreatePoint", "MultiplyTransform"}:
            raise ValueError("unsupported math point method")
        ole = getattr(obj, "_oleobj_", None)
        if ole is None:
            return getattr(obj, name)(argument)
        import pythoncom
        from win32com.client import Dispatch

        result = ole.Invoke(ole.GetIDsOfNames(name), 0, pythoncom.DISPATCH_METHOD, True, argument)
        if result is None:
            raise ValueError("math point method returned no object")
        return Dispatch(result)

    def _set_circle_radius(self, entity_ref: str, expected_radius_mm: float, radius_mm: float) -> dict[str, Any]:
        evidence: dict[str, Any] = {"tool": "sketch.set_circle_radius", "entity_ref": entity_ref,
                                    "mutation_attempted": False, "geometry_verified": False,
                                    "requires_review": True, "geometry_before": None, "geometry_after": None}
        try:
            if (not all(math.isfinite(value) and 0.0001 <= value <= 1_000_000
                        for value in (expected_radius_mm, radius_mm)) or len(entity_ref) != 64):
                raise ValueError("invalid bounded circle edit parameters")
            document = self.require_document(part=True)
            sketch = self._require_bound_active_sketch(document)
            record = self._owned_circles.get(entity_ref)
            if record is None:
                raise ValueError("circle was not verifiably created by this modelling session")
            if record["document"] != self._com_identity(document) or record["sketch"] != self._com_identity(sketch):
                raise ValueError("document or active sketch identity changed")
            before, entities = self._capture_edit_geometry(document, sketch)
            evidence["geometry_before"] = before
            entity = entities.get(entity_ref)
            if entity is None or self._com_identity(entity) != record["entity"]:
                raise ValueError("target circle identity changed")
            target = next(item for item in before["segments"] if item["entity_ref"] == entity_ref)
            if target != record["geometry"]:
                raise ValueError("target circle changed outside the verified task edits")
            if target.get("is_circle") is not True or target["construction"]:
                raise ValueError("only one complete non-construction circle may be repaired")
            if not math.isclose(target["radius_mm"], expected_radius_mm, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("expected radius no longer matches the active circle")
            arc = self._cast_document(entity, "SketchArc")
            # A late-bound proxy can invoke a member while resolving it, so the
            # mutation boundary starts before obtaining the setter attribute.
            evidence["mutation_attempted"] = True
            setter = getattr(arc, "SetRadius")
            accepted = setter(radius_mm / 1000.0)
            # Always re-read after a returned false. A false return is not proof of no mutation.
            if self._com_identity(self.require_document(part=True)) != record["document"]:
                raise ValueError("active document changed during radius edit")
            if self._com_identity(self._require_bound_active_sketch(document)) != record["sketch"]:
                raise ValueError("active sketch changed during radius edit")
            after, _ = self._capture_edit_geometry(document, sketch)
            evidence["geometry_after"] = after
            desired = json.loads(json.dumps(before))
            desired_target = next(item for item in desired["segments"] if item["entity_ref"] == entity_ref)
            desired_target["radius_mm"] = radius_mm
            actual_target = next((item for item in after["segments"] if item["entity_ref"] == entity_ref), None)
            if actual_target is None or not math.isclose(actual_target["radius_mm"], radius_mm, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("circle radius read-back differs from the requested value")
            comparable = json.loads(json.dumps(after))
            next(item for item in comparable["segments"] if item["entity_ref"] == entity_ref)["radius_mm"] = radius_mm
            if accepted not in {True, 1, -1} or comparable != desired:
                raise ValueError("radius edit failed or changed geometry outside the target radius")
            record["geometry"] = actual_target
            evidence.update(geometry_verified=True, requires_review=False, sketch_ref=before["sketch_ref"],
                            old_radius_mm=target["radius_mm"], radius_mm=actual_target["radius_mm"],
                            unchanged_other_entities=True)
            return evidence
        except Exception as exc:
            # No blind rollback: solver side effects and unknown COM outcomes need a safety review.
            raise CircleEditError("圆半径修改未通过安全验证，请停止并检查现场：" + str(exc), evidence) from exc

    def _rectangle(self, cx: float, cy: float, width: float, height: float) -> dict[str, Any]:
        center = self._sketch_point(cx, cy)
        corners = [self._sketch_point(cx + sx * width / 2, cy + sy * height / 2)
                   for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        backend = "native-center-rectangle"

        def create(manager: Any, points: list[Any]) -> list[Any]:
            nonlocal backend
            native_corners = points[1:]
            # CreateCenterRectangle is aligned with native sketch XY, not an
            # arbitrary public face frame. Use four fixed edges when rotated.
            aligned = all(abs(start[0] - end[0]) <= 1e-10 or abs(start[1] - end[1]) <= 1e-10
                          for start, end in zip(native_corners, native_corners[1:] + native_corners[:1], strict=True))
            if aligned:
                return _items(manager.CreateCenterRectangle(*points[0], *native_corners[2]))
            backend = "transformed-four-edges"
            created = []
            for start, end in zip(native_corners, native_corners[1:] + native_corners[:1], strict=True):
                entity = manager.CreateLine(*start, *end)
                if not _created(entity):
                    raise ValueError("SolidWorks 未创建矩形边线；停止剩余边线写入。")
                created.append(entity)
            return created

        def verify(added: list[dict[str, Any]], inverse: Any) -> dict[str, Any]:
            edges = [item for item in added if not item["construction"]]
            actual = self._match_created_edges(edges, corners, inverse, closed=True, construction=False)
            diagonals = [item for item in added if item["construction"]]
            # The native API can also return its two construction diagonals.
            # They are not boundary edges and must not hide unexpected geometry.
            if len(diagonals) not in {0, 2} or (backend == "transformed-four-edges" and diagonals):
                raise ValueError("unexpected rectangle construction geometry")
            remaining = [0, 1]
            for diagonal in diagonals:
                matched = None
                for index in remaining:
                    try:
                        self._match_created_edges([diagonal], [corners[index], corners[index + 2]],
                                                  inverse, closed=False, construction=True)
                        matched = index
                        break
                    except ValueError:
                        continue
                if matched is None:
                    raise ValueError("actual rectangle construction line is not a requested diagonal")
                remaining.remove(matched)
            actual_corner = actual[2]["start_m"]
            actual_center = [(a + b) / 2 for a, b in zip(actual[0]["start_m"], actual_corner, strict=True)]
            return {"center_m": actual_center, "corner_m": actual_corner, "actual_edges_m": actual,
                    "backend": backend, "segment_count": len(added), "boundary_segment_count": 4}

        return self._create_verified_sketch_geometry("sketch.rectangle", [center, *corners], 6, create, verify)

    def _polygon(
        self,
        x_mm: float,
        y_mm: float,
        radius_mm: float,
        sides: int,
        rotation_deg: float,
    ) -> dict[str, Any]:
        document = self.require_document(part=True)
        manager = document.SketchManager
        active_sketch = self._require_bound_active_sketch(document)
        rotation = math.radians(rotation_deg)
        points = [
            self._sketch_point(
                x_mm + radius_mm * math.cos(rotation + 2.0 * math.pi * index / sides),
                y_mm + radius_mm * math.sin(rotation + 2.0 * math.pi * index / sides),
            )
            for index in range(sides)
        ]
        center = self._sketch_point(x_mm, y_mm)
        evidence: dict[str, Any] = {
            "tool": "sketch.polygon", "mutation_attempted": False,
            "geometry_verified": False, "rollback_verified": False, "requires_review": False,
            # Segment restoration is not proof of restored constraints, selection,
            # or the full document. The outer transaction must check those too.
            "rollback_scope": "new_sketch_segments", "document_state_restored": False,
        }
        created_entities: list[Any] = []
        try:
            before, _ = self._capture_edit_geometry(document, active_sketch)
            if len(before["segments"]) + sides + 1 > 128:
                raise ValueError("polygon would exceed the fully readable sketch segment limit")
            add_to_db = _read_member(manager, "AddToDB")
            display_when_added = _read_member(manager, "DisplayWhenAdded")
            if any(value not in {True, False, 0, 1, -1} for value in (add_to_db, display_when_added)):
                raise ValueError("sketch manager flags are unknown")
            transform = _read_member(active_sketch, "ModelToSketchTransform")
            transform_data = self._strict_vector(_read_member(transform, "ArrayData"), 16)
            inverse = _read_member(transform, "Inverse")
            local_center = self._polygon_transform_point(center, transform)
            local_points = [self._polygon_transform_point(point, transform) for point in points]
            for model_point, local_point in zip([center, *points], [local_center, *local_points], strict=True):
                if abs(local_point[2]) > 1e-8 or not self._polygon_points_close(
                    self._polygon_transform_point(local_point, inverse), model_point
                ):
                    raise ValueError("requested polygon is not in the active sketch plane")
        except Exception as exc:
            raise PolygonCreationError(f"无法建立正多边形完整几何校验，未执行写入：{exc}", evidence) from exc

        def capture() -> tuple[dict[str, Any], dict[str, Any]]:
            current_document = self.require_document(part=True)
            if self._com_identity(current_document) != before["document_identity"]:
                raise ValueError("active document changed")
            current_sketch = self._require_bound_active_sketch(current_document)
            if self._com_identity(current_sketch) != self._com_identity(active_sketch):
                raise ValueError("active sketch identity changed")
            current_transform = self._strict_vector(
                _read_member(_read_member(active_sketch, "ModelToSketchTransform"), "ArrayData"), 16
            )
            if current_transform != transform_data:
                raise ValueError("active sketch transform changed")
            current, entities = self._capture_edit_geometry(document, active_sketch)
            if any(current[key] != before[key] for key in (
                "document_identity", "sketch_ref", "sketch_name", "configuration"
            )):
                raise ValueError("polygon document or sketch boundary changed")
            return current, entities

        def fail(message: str, exc: Exception | None = None, *, flags_restored: bool = True) -> None:
            restored = self._rollback_polygon_geometry(document, active_sketch, before, capture, created_entities)
            evidence.update(rollback_verified=restored, requires_review=not (restored and flags_restored),
                            sketch_manager_flags_restored=flags_restored,
                            verification_error=str(exc)[:350] if exc else message[:350])
            detail = "已自动回滚并复核本次新增草图实体。" if restored else (
                "自动回滚无法验证，已停止后续写入，请人工检查当前草图。"
            )
            raise PolygonCreationError(f"{message}；{detail}", evidence) from exc

        def prove(backend: str, attempts: int) -> dict[str, Any] | None:
            after, _ = capture()
            if after == before:
                return None
            verified = self._verify_polygon_geometry(before, after, points, center, radius_mm, inverse)
            return {
                **evidence, **verified, "frame": self._frame_evidence(),
                "backend": backend, "commit_attempts": attempts,
                "geometry_verified": True, "creation_verified": True, "requires_review": False,
                "geometry_source": "ISketchLine endpoints and ISketch.ModelToSketchTransform",
            }

        try:
            native_method = getattr(manager, "CreatePolygon", None)
        except Exception:
            native_method = None
        if callable(native_method):
            # A returned proxy or segment count never proves coordinates. Native
            # polygon coordinates belong to the actual sketch, not our public
            # frame. Retry only a fully observed no-op; never stack duplicates.
            for native_attempt in range(2):
                native_error: Exception | None = None
                try:
                    evidence["mutation_attempted"] = True
                    created_entities.extend(_items(native_method(*local_center, *local_points[0], sides, True)))
                except Exception as exc:
                    native_error = exc
                self._settled_sketch_segment_growth(document, active_sketch, len(before["segments"]), expected=sides)
                try:
                    result = prove("solidworks-native", native_attempt + 1)
                    if result is not None:
                        return result
                except Exception as exc:
                    # New-segment cleanup alone cannot prove that native
                    # constraints/solver state were restored. Let the host
                    # checkpoint verify the whole state before another write.
                    fail(f"原生正多边形实际几何复核失败：{exc}", exc)
                if native_attempt == 0:
                    document.ClearSelection2(True)
                if native_error is not None and native_attempt == 1:
                    break

        # Fixed compatibility path, subject to exactly the same real-geometry
        # proof. Do not accept wrappers that cannot enumerate/read their lines.
        fallback_error: Exception | None = None
        flags_restored = False
        try:
            manager.AddToDB = True
            manager.DisplayWhenAdded = False
            for index, start in enumerate(local_points):
                end = local_points[(index + 1) % len(local_points)]
                evidence["mutation_attempted"] = True
                entity = manager.CreateLine(*start, *end)
                if not _created(entity):
                    raise ValueError(f"第 {index + 1} 条边未返回实体")
                created_entities.append(entity)
        except Exception as exc:
            fallback_error = exc
        finally:
            try:
                manager.AddToDB = bool(add_to_db)
                manager.DisplayWhenAdded = bool(display_when_added)
                flags_restored = (
                    _read_member(manager, "AddToDB") == add_to_db
                    and _read_member(manager, "DisplayWhenAdded") == display_when_added
                )
            except Exception:
                pass
        self._settled_sketch_segment_growth(document, active_sketch, len(before["segments"]), expected=sides)
        if not flags_restored:
            fail("草图管理器选项未恢复，不能继续写入", fallback_error, flags_restored=False)
        if fallback_error is not None:
            fail(f"SolidWorks 正多边形逐边创建失败：{fallback_error}", fallback_error)
        try:
            result = prove("line-fallback", 1)
            if result is None:
                raise ValueError("没有实际提交任何正多边形实体")
            return result
        except Exception as exc:
            fail(f"SolidWorks 正多边形实际几何复核失败：{exc}", exc)
        raise AssertionError("polygon verification cannot fall through")

    @staticmethod
    def _polygon_points_close(first: Any, second: Any) -> bool:
        return all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8)
                   for a, b in zip(first, second, strict=True))

    def _polygon_transform_point(self, coordinates: Any, transform: Any) -> list[float]:
        import pythoncom
        from win32com.client import VARIANT

        expected = self._strict_vector(coordinates, 3)
        utility = _read_member(self._sw, "GetMathUtility")
        point = self._invoke_math_point_method(
            utility, "CreatePoint", VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, expected)
        )
        if not self._polygon_points_close(self._strict_vector(_read_member(point, "ArrayData"), 3), expected):
            raise ValueError("math point input was not committed")
        result = self._invoke_math_point_method(point, "MultiplyTransform", transform)
        return self._strict_vector(_read_member(result, "ArrayData"), 3)

    def _verify_polygon_geometry(self, before: dict[str, Any], after: dict[str, Any],
                                 points: Any, center: Any, radius_mm: float, inverse: Any) -> dict[str, Any]:
        old = {item["entity_ref"]: item for item in before["segments"]}
        current = {item["entity_ref"]: item for item in after["segments"]}
        if any(current.get(key) != item for key, item in old.items()):
            raise ValueError("pre-existing sketch geometry changed")
        added = [item for key, item in current.items() if key not in old]
        edges = [item for item in added if not item["construction"]]
        construction = [item for item in added if item["construction"]]
        if len(edges) != len(points) or len(construction) > 1:
            raise ValueError("polygon did not create exactly the requested edge count")
        remaining = list(range(len(points)))
        actual_vertices: dict[int, list[float]] = {}
        actual_edges: list[dict[str, Any]] = []
        for edge in edges:
            if edge["type"] != 0:
                raise ValueError("polygon includes a non-line boundary")
            start = self._polygon_transform_point(edge["start_sketch_m"], inverse)
            end = self._polygon_transform_point(edge["end_sketch_m"], inverse)
            matches = []
            for index in remaining:
                wanted_start, wanted_end = points[index], points[(index + 1) % len(points)]
                if self._polygon_points_close(start, wanted_start) and self._polygon_points_close(end, wanted_end):
                    matches.append((index, start))
                elif self._polygon_points_close(end, wanted_start) and self._polygon_points_close(start, wanted_end):
                    matches.append((index, end))
            if len(matches) != 1:
                raise ValueError("actual polygon edge does not match requested model-space endpoints")
            index, actual_start = matches[0]
            remaining.remove(index)
            actual_vertices[index] = actual_start
            actual_edges.append({"entity_ref": edge["entity_ref"], "start_m": start, "end_m": end})
        for item in construction:
            actual_center = self._polygon_transform_point(item.get("center_sketch_m"), inverse)
            permitted_radii = (radius_mm, radius_mm * math.cos(math.pi / len(points)))
            if (item.get("is_circle") is not True or not self._polygon_points_close(actual_center, center)
                    or not any(math.isclose(item["radius_mm"], radius, rel_tol=1e-9, abs_tol=1e-6)
                               for radius in permitted_radii)):
                raise ValueError("unexpected polygon construction geometry")
        return {"points_m": [actual_vertices[index] for index in range(len(points))],
                "actual_edges_m": actual_edges, "segment_count": len(added),
                "boundary_segment_count": len(edges), "sketch_ref": after["sketch_ref"]}

    def _rollback_polygon_geometry(self, document: Any, sketch: Any, before: dict[str, Any],
                                   capture: Any, created_entities: list[Any]) -> bool:
        """Delete only identity-proven new segments, never an enumeration suffix or undo history."""
        selection_changed = False
        try:
            current, entities = capture()
            old = {item["entity_ref"]: item for item in before["segments"]}
            observed = {item["entity_ref"]: item for item in current["segments"]}
            if any(observed.get(key) != item for key, item in old.items()):
                return False
            added = [entity for key, entity in entities.items() if key not in old]
            if not added:
                return current == before
            # A before/after delta alone does not establish ownership: manual
            # edits can race a COM call. Every deletion target must also have
            # been returned by this command's own creation operation.
            owned = {self._com_identity(entity) for entity in created_entities if _created(entity)}
            if any(self._com_identity(entity) not in owned for entity in added):
                return False
            selection_changed = True
            document.ClearSelection2(True)
            null_dispatch = self._null_dispatch()
            for index, entity in enumerate(added):
                selected = False
                for name, arguments in (
                    ("Select4", (index > 0, null_dispatch)),
                    ("Select2", (index > 0, 0)),
                    ("Select", (index > 0,)),
                ):
                    try:
                        if bool(_read_member(entity, name, *arguments)):
                            selected = True
                            break
                    except Exception:
                        continue
                if not selected:
                    return False
            if document.SelectionManager.GetSelectedObjectCount2(-1) != len(added):
                return False
            # Count alone would permit a stale/wrong selection to delete user
            # geometry. Re-read all selected COM identities before EditDelete.
            actual = {
                self._com_identity(document.SelectionManager.GetSelectedObject6(index + 1, -1))
                for index in range(len(added))
            }
            if actual != {self._com_identity(entity) for entity in added}:
                return False
            document.EditDelete()
            restored, _ = capture()
            return restored == before
        except Exception:
            return False
        finally:
            if selection_changed:
                try:
                    document.ClearSelection2(True)
                except Exception:
                    pass

    @staticmethod
    def _settled_sketch_segment_growth(
        document: Any,
        active_sketch: Any,
        before_count: int,
        *,
        expected: int,
    ) -> int | None:
        """Return committed segment growth after bounded SolidWorks settling.

        ``None`` means the automation wrapper cannot enumerate sketch
        segments.  A numeric result is observable topology and is therefore
        safe to use for retry/rollback decisions.
        """

        try:
            member = getattr(active_sketch, "GetSketchSegments")
        except Exception:
            return None
        if not callable(member):
            return None

        def growth() -> int:
            try:
                return max(0, len(_items(member())) - before_count)
            except Exception:
                return 0

        observed = growth()
        if observed >= expected:
            return observed
        try:
            document.GraphicsRedraw2()
        except Exception:
            pass
        observed = max(observed, growth())
        if observed >= expected:
            return observed
        try:
            document.ForceRebuild3(False)
        except Exception:
            pass
        return max(observed, growth())

    @staticmethod
    def _rollback_sketch_segments(
        document: Any,
        active_sketch: Any,
        before_count: int,
        *,
        undo_steps: int,
    ) -> bool:
        """Remove only sketch entities added by the current command.

        Direct selection/deletion is preferred because it cannot cross the
        command boundary.  EditUndo2 is a bounded compatibility fallback, and
        the entity count is always read back before reporting success.
        """

        def count() -> int:
            return len(_items(_value(active_sketch, "GetSketchSegments")))

        current = _items(_value(active_sketch, "GetSketchSegments"))
        if len(current) <= before_count:
            return True
        added = current[before_count:]
        try:
            document.ClearSelection2(True)
        except Exception:
            pass
        selected = 0
        for entity in added:
            append = selected > 0
            accepted = False
            for name, arguments in (
                ("Select4", (append, None)),
                ("Select2", (append, 0)),
                ("Select", (append,)),
            ):
                method = getattr(entity, name, None)
                if not callable(method):
                    continue
                try:
                    accepted = method(*arguments) is not False
                except Exception:
                    accepted = False
                if accepted:
                    break
            if accepted:
                selected += 1
        if selected == len(added):
            try:
                document.EditDelete()
            except Exception:
                pass
            if count() == before_count:
                try:
                    document.ClearSelection2(True)
                except Exception:
                    pass
                return True

        try:
            document.ClearSelection2(True)
        except Exception:
            pass
        try:
            undone = document.EditUndo2(max(1, int(undo_steps)))
            if undone is False:
                return False
        except Exception:
            return False
        try:
            document.ForceRebuild3(False)
        except Exception:
            pass
        return count() == before_count

    def _line(
        self,
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        *,
        centerline: bool,
    ) -> dict[str, Any]:
        if x1_mm == x2_mm and y1_mm == y2_mm:
            raise ToolError("草图直线的两个端点不能重合。")
        start = self._sketch_point(x1_mm, y1_mm)
        end = self._sketch_point(x2_mm, y2_mm)
        if centerline:
            self._last_centerline = None

        def create(manager: Any, points: list[Any]) -> list[Any]:
            method = manager.CreateCenterLine if centerline else manager.CreateLine
            return [method(*points[0], *points[1])]

        def verify(added: list[dict[str, Any]], inverse: Any) -> dict[str, Any]:
            actual = self._match_created_edges(added, [start, end], inverse, closed=False, construction=centerline)
            return {"start_m": actual[0]["start_m"], "end_m": actual[0]["end_m"], "actual_edges_m": actual}

        def register(document: Any, sketch: Any, created: list[Any], prior: dict[str, Any],
                     added: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
            if centerline:
                # Revolve signatures use native sketch coordinates. The cast
                # exposes actual ISketchLine endpoints; never substitute world
                # request coordinates for an unreadable native endpoint.
                signature = capture_sketch_line_signature(
                    document, sketch, self._cast_document(created[0], "SketchLine"))
                if (not signature.construction or signature.segment_type != 0
                        or not self._polygon_points_close(signature.start_m, added[0]["start_sketch_m"])
                        or not self._polygon_points_close(signature.end_m, added[0]["end_sketch_m"])):
                    raise ValueError("centerline signature differs from independently verified native endpoints")
                self._last_centerline = signature
            return {}

        return self._create_verified_sketch_geometry(
            "sketch.centerline" if centerline else "sketch.line", [start, end], 1, create, verify, register)

    @staticmethod
    def _sketch_check_result(
        result: Any, open_reference: Any = None, closed_reference: Any = None
    ) -> tuple[int, int | None, int | None]:
        values = _items(result)
        if len(values) >= 3:
            return int(values[0]), int(values[1]), int(values[2])
        if len(values) != 1:
            raise ValueError("CheckFeatureUse 返回值格式无法识别")
        open_count = getattr(open_reference, "value", None)
        closed_count = getattr(closed_reference, "value", None)
        return (
            int(values[0]),
            int(open_count) if open_count is not None else None,
            int(closed_count) if closed_count is not None else None,
        )

    @classmethod
    def _check_sketch_profile(
        cls, sketch: Any, feature_kind: str, operation: str
    ) -> dict[str, Any]:
        usage, usage_name = SKETCH_FEATURE_USAGE[(feature_kind, operation)]
        try:
            method = getattr(sketch, "CheckFeatureUse")
        except Exception:
            method = None
        if not callable(method):
            raise ToolError(
                "当前 SolidWorks COM 包装未暴露 ISketch.CheckFeatureUse，"
                f"无法安全验证 {usage_name} 轮廓。"
            )

        first_error: Exception | None = None
        try:
            import pythoncom
            from win32com.client import VARIANT

            open_reference = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            closed_reference = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            result = method(usage, open_reference, closed_reference)
            status, open_count, closed_count = cls._sketch_check_result(
                result, open_reference, closed_reference
            )
        except Exception as exc:
            first_error = exc
            try:
                result = method(usage, 0, 0)
                status, open_count, closed_count = cls._sketch_check_result(result)
            except Exception as fallback_exc:
                raise ToolError(
                    "SolidWorks 草图特征适用性预检调用失败："
                    f"{fallback_exc or first_error}"
                ) from fallback_exc

        if status != 0:
            detail = SKETCH_CHECK_STATUS.get(status, f"状态码 {status}")
            raise ToolError(f"草图不能用于 {usage_name}：{detail}。")

        if closed_count is None:
            try:
                closed_count = int(_value(sketch, "GetSketchRegionCount"))
            except (TypeError, ValueError):
                closed_count = None
        if closed_count == 0:
            raise ToolError(f"草图不能用于 {usage_name}：没有闭合轮廓。")
        return {
            "available": True,
            "usage": usage_name,
            "status": status,
            "open_contours": open_count,
            "closed_contours": closed_count,
        }

    def _extrude(
        self,
        *,
        depth_mm: float,
        operation: str,
        end_condition: str,
        reverse: bool,
        merge: bool,
    ) -> dict[str, Any]:
        document = self.require_document(part=True)
        manager = document.SketchManager
        active_sketch = self._require_bound_active_sketch(document)
        profile_check = self._check_sketch_profile(active_sketch, "extrude", operation)
        manager.InsertSketch(True)
        if _value(manager, "ActiveSketch") is not None:
            raise ToolError("SolidWorks 未退出拉伸轮廓草图，不能安全创建拉伸特征。")
        self._sketch_session = None
        depth = depth_mm / 1000.0
        end_type = SW_END_MID_PLANE if end_condition == "mid_plane" else SW_END_BLIND
        feature_manager = document.FeatureManager
        if operation == "boss":
            method = getattr(feature_manager, "FeatureExtrusion3", None)
            if method is None:
                method = feature_manager.FeatureExtrusion2
            feature = method(
                True,
                False,
                reverse,
                end_type,
                SW_END_BLIND,
                depth,
                0.0,
                False,
                False,
                False,
                False,
                0.0,
                0.0,
                False,
                False,
                False,
                False,
                merge,
                False,
                True,
                0,
                0.0,
                False,
            )
        else:
            feature = feature_manager.FeatureCut4(
                True,
                False,
                reverse,
                end_type,
                SW_END_BLIND,
                depth,
                depth,
                False,
                False,
                False,
                False,
                0.0,
                0.0,
                False,
                False,
                False,
                False,
                False,
                False,
                True,
                False,
                False,
                False,
                0,
                0.0,
                False,
                True,
            )
        if not _created(feature):
            raise ToolError(f"SolidWorks 未创建拉伸 {operation} 特征。")
        try:
            rebuilt = document.ForceRebuild3(False)
            if rebuilt is False:
                raise ToolError(f"SolidWorks 创建拉伸 {operation} 后重建失败。")
            applied = self._extrude_feature_evidence(feature, document)
            applied["feature_health"] = self._feature_health(feature)
            applied["profile_check"] = profile_check
            if (
                not math.isclose(applied["depth_m"], depth, rel_tol=1e-7, abs_tol=1e-9)
                or applied["operation"] != operation
                or applied["end_condition"] != end_condition
                or applied["reverse"] is not reverse
                or (operation == "boss" and applied["merge"] is not merge)
            ):
                raise ToolError("SolidWorks 创建的拉伸特征参数与请求不一致。")
            terminal = self._remember_terminal_face(feature, depth, reverse=reverse)
            applied["terminal_face"] = terminal
            document.GraphicsRedraw2()
        except Exception as exc:
            self._last_terminal_signature = None
            self._last_terminal_frame = None
            self._last_terminal_error = str(exc)
            if not self._undo_last_feature(document):
                raise ToolError(f"{exc}；自动回滚失败，请人工检查当前模型。") from exc
            raise
        self._last_centerline = None
        self._pending_revolve = None
        return {"tool": "feature.extrude", **applied}

    @staticmethod
    def _undo_last_feature(document: Any) -> bool:
        try:
            undone = document.EditUndo2(1)
            if undone is False:
                return False
            rebuilt = document.ForceRebuild3(False)
            return rebuilt is not False
        except Exception:
            return False

    def _extrude_feature_evidence(self, feature: Any, document: Any) -> dict[str, Any]:
        definition = _value(feature, "GetDefinition")
        if definition is None:
            raise ToolError("SolidWorks 未返回可核对的拉伸特征定义。")
        accessed = False
        try:
            accessed = bool(
                definition.AccessSelections(document, self._null_dispatch())
            )
            if not accessed:
                raise ToolError("SolidWorks 无法读取拉伸特征定义。")
            end_code = int(definition.GetEndCondition(True))
            end_condition = {
                SW_END_BLIND: "blind",
                SW_END_MID_PLANE: "mid_plane",
            }.get(end_code, f"unsupported:{end_code}")
            return {
                "depth_m": float(definition.GetDepth(True)),
                "operation": self._feature_operation(feature),
                "end_condition": end_condition,
                "reverse": bool(definition.ReverseDirection),
                "merge": bool(definition.Merge),
            }
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"读取拉伸特征定义失败：{exc}") from exc
        finally:
            if accessed:
                try:
                    definition.ReleaseSelectionAccess()
                except Exception:
                    pass

    @staticmethod
    def _feature_operation(feature: Any) -> str:
        type_name = str(_value(feature, "GetTypeName2", "") or "").casefold()
        # Instant3D wraps the real feature type as ``ICE``.  SOLIDWORKS documents
        # GetTypeName (without the 2 suffix) as the way to obtain the underlying
        # Boss/Cut/Revolution type in that case.
        if type_name == "ice":
            type_name = str(_value(feature, "GetTypeName", "") or "").casefold()
        if "cut" in type_name:
            return "cut"
        if any(token in type_name for token in ("boss", "extr", "revol")):
            return "boss"
        raise ToolError(f"无法识别 SolidWorks 特征类型：{type_name or '<空>'}。")

    def _feature_health(self, feature: Any) -> dict[str, Any]:
        try:
            method = getattr(feature, "GetErrorCode2")
        except Exception:
            method = None
        if not callable(method):
            return {"available": False, "error_code": None, "warning": None}

        warning_reference: Any = None
        try:
            warning_reference = self._bool_reference()
            result = method(warning_reference)
            values = _items(result)
            error_code = int(values[0])
            warning = bool(getattr(warning_reference, "value", False))
        except Exception as first_error:
            try:
                values = _items(method(False))
                error_code = int(values[0])
                warning = bool(values[1]) if len(values) > 1 else False
            except Exception as fallback_error:
                raise ToolError(
                    "SolidWorks 特征错误状态读取失败："
                    f"{fallback_error or first_error}"
                ) from fallback_error
        if error_code != 0:
            kind = "警告" if warning else "错误"
            raise ToolError(f"SolidWorks 特征重建{kind}码：{error_code}。")
        return {"available": True, "error_code": 0, "warning": False}

    def _remember_terminal_face(
        self, feature: Any, depth_m: float, *, reverse: bool = False
    ) -> dict[str, Any] | None:
        frame = self._sketch_frame
        document = self.require_document(part=True)
        try:
            signature = capture_terminal_face_signature(
                document,
                feature,
                frame,
                depth_m,
                reverse=reverse,
            )
        except TopologyResolutionError as exc:
            self._last_terminal_signature = None
            self._last_terminal_frame = None
            self._last_terminal_error = str(exc)
            return None
        center = signature.center_m
        terminal_normal = signature.normal
        u = tuple(frame.get("u", (1.0, 0.0, 0.0)))
        v = tuple(frame.get("v", (0.0, 1.0, 0.0)))
        origin_xy = (
            sum(center[index] * u[index] for index in range(3)),
            sum(center[index] * v[index] for index in range(3)),
        )
        self._last_terminal_signature = signature
        self._last_terminal_frame = {
            "origin": center,
            "origin_xy": origin_xy,
            "u": u,
            "v": v,
            "normal": terminal_normal,
        }
        self._last_terminal_error = ""
        return signature.to_dict()

    def _revolve(
        self,
        *,
        angle_deg: float,
        operation: str,
        reverse: bool,
        merge: bool,
    ) -> dict[str, Any]:
        document = self.require_document(part=True)
        manager = document.SketchManager
        active_sketch = _value(manager, "ActiveSketch")
        if active_sketch is not None:
            active_sketch = self._require_bound_active_sketch(document)
            profile_check = self._check_sketch_profile(
                active_sketch, "revolve", operation
            )
            signature = self._last_centerline
            if signature is None:
                raise ToolError("旋转需要当前草图轮廓和一条由 sketch.centerline 创建的中心线。")
            self._pending_revolve = signature
            manager.InsertSketch(True)
            if _value(manager, "ActiveSketch") is not None:
                raise ToolError("SolidWorks 未退出旋转轮廓草图，不能安全创建旋转特征。")
            self._sketch_session = None
        elif self._pending_revolve is not None:
            signature = self._pending_revolve
            profile_check = None
        else:
            raise ToolError("当前没有可用于旋转的活动草图。")
        rebuilt = document.ForceRebuild3(False)
        if rebuilt is False:
            raise ToolError("解析旋转中心线前 SolidWorks 重建失败。")
        try:
            resolved_axis = resolve_sketch_line(
                document,
                signature,
                lambda payload: self._object_by_persistent_reference(
                    document.Extension, payload
                ),
            )
        except TopologyResolutionError as exc:
            raise ToolError(f"旋转中心线解析失败：{exc}") from exc
        sketch_feature = resolved_axis.sketch_feature
        axis = resolved_axis.line
        if profile_check is None:
            closed_sketch = _value(sketch_feature, "GetSpecificFeature2")
            if closed_sketch is None:
                raise ToolError("无法重新取得旋转草图进行轮廓预检。")
            profile_check = self._check_sketch_profile(
                closed_sketch, "revolve", operation
            )
        document.ClearSelection2(True)
        if not self._select_with_mark(document, sketch_feature, False, 0):
            raise ToolError("SolidWorks 无法选中旋转草图。")
        if not self._select_with_mark(document, axis, True, 4):
            raise ToolError("SolidWorks 无法选中旋转中心线。")
        selection_manager = document.SelectionManager
        selection_counts = {
            "all": int(selection_manager.GetSelectedObjectCount2(-1) or 0),
            "profile_mark_0": int(selection_manager.GetSelectedObjectCount2(0) or 0),
            "axis_mark_4": int(selection_manager.GetSelectedObjectCount2(4) or 0),
        }
        if selection_counts != {"all": 2, "profile_mark_0": 1, "axis_mark_4": 1}:
            document.ClearSelection2(True)
            raise ToolError(
                "旋转选择集合不符合 API 约定："
                f"总数={selection_counts['all']}，Mark=0 草图="
                f"{selection_counts['profile_mark_0']}，Mark=4 中心线="
                f"{selection_counts['axis_mark_4']}。"
            )
        feature = document.FeatureManager.FeatureRevolve2(
            True,
            True,
            False,
            operation == "cut",
            reverse,
            False,
            SW_END_BLIND,
            SW_END_BLIND,
            math.radians(angle_deg),
            0.0,
            False,
            False,
            0.0,
            0.0,
            0,
            0.0,
            0.0,
            merge,
            False,
            True,
        )
        if not _created(feature):
            raise ToolError(f"SolidWorks 未创建旋转 {operation} 特征。")
        try:
            rebuilt = document.ForceRebuild3(False)
            if rebuilt is False:
                raise ToolError(f"SolidWorks 创建旋转 {operation} 后重建失败。")
            applied = self._revolve_feature_evidence(feature, document)
            applied["feature_health"] = self._feature_health(feature)
            applied["profile_check"] = profile_check
            expected_angle = math.radians(angle_deg)
            if (
                not math.isclose(
                    applied["angle_radians"], expected_angle, rel_tol=1e-7, abs_tol=1e-9
                )
                or applied["operation"] != operation
                or applied["reverse"] is not reverse
                or (operation == "boss" and applied["merge"] is not merge)
            ):
                raise ToolError("SolidWorks 创建的旋转特征参数与请求不一致。")
            document.GraphicsRedraw2()
        except Exception as exc:
            if not self._undo_last_feature(document):
                raise ToolError(f"{exc}；自动回滚失败，请人工检查当前模型。") from exc
            raise
        self._last_centerline = None
        self._pending_revolve = None
        self._sketch_session = None
        self._last_terminal_signature = None
        self._last_terminal_frame = None
        self._last_terminal_error = "旋转特征改变了拓扑，需要重新捕获目标面。"
        return {
            "tool": "feature.revolve",
            "axis_resolution": {
                "strategy": resolved_axis.strategy,
                "candidate_count": resolved_axis.candidate_count,
                "selection_counts": selection_counts,
            },
            **applied,
        }

    def _revolve_feature_evidence(self, feature: Any, document: Any) -> dict[str, Any]:
        definition = _value(feature, "GetDefinition")
        if definition is None:
            raise ToolError("SolidWorks 未返回可核对的旋转特征定义。")
        accessed = False
        try:
            accessed = bool(
                definition.AccessSelections(document, self._null_dispatch())
            )
            if not accessed:
                raise ToolError("SolidWorks 无法读取旋转特征定义。")
            return {
                "angle_radians": float(definition.GetRevolutionAngle(True)),
                "operation": self._feature_operation(feature),
                "reverse": bool(definition.ReverseDirection),
                "merge": bool(definition.Merge),
            }
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"读取旋转特征定义失败：{exc}") from exc
        finally:
            if accessed:
                try:
                    definition.ReleaseSelectionAccess()
                except Exception:
                    pass

    @staticmethod
    def _select_mate_entity(document: Any, value: Any, append: bool) -> bool:
        selection_manager = document.SelectionManager
        try:
            value.Select4(append, None)
            index = int(selection_manager.GetSelectedObjectCount2(-1) or 0)
            if index > 0 and bool(selection_manager.SetSelectedObjectMark(index, 1, 0)):
                return True
        except Exception:
            pass
        try:
            select_data = selection_manager.CreateSelectData()
            select_data.Mark = 1
            if bool(selection_manager.AddSelectionListObject(value, select_data)):
                return True
        except Exception:
            pass
        try:
            if bool(value.SelectByMark(append, 1)):
                return True
        except Exception:
            pass
        try:
            select_data = document.SelectionManager.CreateSelectData()
            select_data.Mark = 1
            return bool(value.Select4(append, select_data))
        except Exception:
            return False

    @staticmethod
    def _mate_entities_array(first: Any, second: Any) -> Any:
        """Build the SAFEARRAY(IDispatch) required by IMateFeatureData."""

        try:
            import pythoncom
            from win32com.client import VARIANT

            return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (first, second))
        except Exception:
            # Test doubles and late-bound COM implementations accept a normal sequence.
            return (first, second)

    @staticmethod
    def _select_with_mark(document: Any, value: Any, append: bool, mark: int) -> bool:
        try:
            if bool(value.Select2(append, mark)):
                return True
        except Exception:
            pass
        try:
            select_data = document.SelectionManager.CreateSelectData()
            select_data.Mark = mark
            return bool(value.Select4(append, select_data))
        except Exception:
            return False

    def _investigation_document(self, probe: _ReadProbe) -> tuple[Any, dict[str, Any]]:
        data: dict[str, Any] = {"connected": self._sw is not None, "document_available": False}
        if self._sw is None:
            return None, data
        document = probe.get(self._sw, "ActiveDoc")
        if document is None:
            data["document_available"] = None if probe.issues else False
            return None, data
        data["document_available"] = True
        title = probe.get(document, "GetTitle")
        if not isinstance(title, str) or not title:
            probe.issue("document title is unavailable")
        data["document_title"] = title[:250] if isinstance(title, str) else None
        code = probe.attempt("GetType", lambda: int(_read_member(document, "GetType")))
        data["document_type"] = {1: "part", 2: "assembly", 3: "drawing"}.get(code, "unknown")
        if data["document_type"] == "unknown":
            probe.issue("document type is unknown")
        return document, data

    def _inspect_capabilities(self) -> dict[str, Any]:
        from ..tools import build_tool_registry

        probe = _ReadProbe()
        document, data = self._investigation_document(probe)
        revision = probe.get(self._sw, "RevisionNumber") if self._sw is not None else None
        if self._sw is not None and (not isinstance(revision, str) or not revision):
            probe.issue("SolidWorks revision is unavailable")
            revision = None
        members = {
            "application": (self._sw, ("NewDocument", "RevisionNumber")),
            "document": (document, ("GetBodies2", "FirstFeature", "FeatureByName")),
            "sketch_manager": (_value(document, "SketchManager"), ("InsertSketch", "CreateCircleByRadius")),
            "feature_manager": (_value(document, "FeatureManager"), ("FeatureExtrusion3", "FeatureRevolve2")),
        }
        availability = []
        for scope, (owner, names) in members.items():
            for name in names:
                status = "unknown"
                if owner is not None:
                    try:
                        member = getattr(owner, name)
                        status = "present" if member is not None else "not_exposed"
                    except AttributeError:
                        status = "not_exposed"
                    except Exception:
                        status = "unknown"
                availability.append({"scope": scope, "member": name, "status": status})
        data.update(
            solidworks_revision=revision,
            implemented_tools=[{"name": item["name"], "write_scope": item["write_scope"]}
                               for item in build_tool_registry().describe()],
            api_members=availability,
            operation_success_verified=False,
            note="成员可见性仅是运行能力线索；未执行建模，不保证操作成功。工具列表表示软件实现，"
                 "不扩展当前任务授权。未连接时版本未知。",
        )
        return probe.finish(data)

    def _read_persistent_reference(self, document: Any, entity: Any, probe: _ReadProbe) -> str | None:
        def read() -> str:
            raw = _read_member(_read_member(document, "Extension"), "GetPersistReference3", entity)
            payload = self._reference_bytes(raw)
            if not 0 < len(payload) <= 1024:
                raise ValueError("persistent reference is empty or too large")
            return base64.b64encode(payload).decode("ascii")
        return probe.attempt("GetPersistReference3", read)

    @staticmethod
    def _strict_vector(value: Any, size: int) -> list[float]:
        values = _items(value)
        if len(values) != size:
            raise ValueError("invalid vector length")
        result = [float(item) for item in values]
        if not all(math.isfinite(item) for item in result):
            raise ValueError("nonfinite vector")
        return result

    def _read_planar_reference(self, document: Any, face: Any, probe: _ReadProbe) -> dict[str, Any] | None:
        def geometry() -> dict[str, Any] | None:
            surface = _read_member(face, "GetSurface")
            planar = _read_member(surface, "IsPlane")
            if planar is None:
                raise ValueError("surface type is unknown")
            if not planar:
                return None
            box = self._strict_vector(_read_member(face, "GetBox"), 6)
            normal = self._strict_vector(_read_member(face, "Normal"), 3)
            length = math.sqrt(sum(value * value for value in normal))
            area = float(_read_member(face, "GetArea"))
            if not math.isfinite(length) or length <= 1e-15 or not math.isfinite(area) or area <= 0:
                raise ValueError("invalid planar face geometry")
            if any(box[index] > box[index + 3] for index in range(3)):
                raise ValueError("invalid face bounding box")
            return {"kind": "planar_face", "normal": [value / length for value in normal],
                    "box_m": box, "center_m": [box[i] / 2 + box[i + 3] / 2 for i in range(3)],
                    "center_kind": "bounding_box", "area_m2": area}
        candidate = probe.attempt("planar face geometry", geometry)
        if candidate is not None:
            candidate["persist_reference"] = self._read_persistent_reference(document, face, probe)
            candidate["exact_reference_available"] = candidate["persist_reference"] is not None
        return candidate

    def _inspect_references(self, limit: int) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ToolError("候选参考数量必须位于 1 到 100。")
        probe = _ReadProbe()
        document, data = self._investigation_document(probe)
        references: list[dict[str, Any]] = []
        data.update(references=references, truncated=False, limit=limit)
        if document is None:
            return probe.finish(data)

        def append(candidate: dict[str, Any]) -> bool:
            if len(references) == limit:
                data["truncated"] = True
                probe.issue("reference result limit reached")
                return False
            references.append(candidate)
            return True

        feature = probe.get(document, "FirstFeature")
        visited: set[str] = set()
        for _ in range(3000):
            if feature is None:
                break
            name = probe.get(feature, "Name")
            if not isinstance(name, str) or not 1 <= len(name) <= 250 or name in visited:
                probe.issue("feature enumeration is cyclic or ambiguous")
                break
            visited.add(name)
            if probe.get(feature, "GetTypeName2") == "RefPlane":
                reference = self._read_persistent_reference(document, feature, probe)
                canonical = next((key for key, aliases in PLANE_NAMES.items() if name in aliases), None)
                if not append({"kind": "reference_plane", "name": name, "canonical": canonical,
                               "persist_reference": reference, "exact_reference_available": reference is not None}):
                    return probe.finish(data)
            feature = probe.get(feature, "GetNextFeature")
        else:
            probe.issue("feature enumeration limit reached")
        if data.get("document_type") != "part":
            data["face_scope"] = "solid_body_faces_only; current document is not a part"
            return probe.finish(data)
        bodies = _items(probe.get(document, "GetBodies2", 0, False))
        if len(bodies) > 256:
            probe.issue("body enumeration limit reached")
            data["truncated"] = True
        visited_faces = 0
        for body in bodies[:256]:
            faces = _items(probe.get(body, "GetFaces"))
            for face in faces:
                visited_faces += 1
                if visited_faces > 5000:
                    probe.issue("face enumeration limit reached")
                    data["truncated"] = True
                    return probe.finish(data)
                candidate = self._read_planar_reference(document, face, probe)
                if candidate is not None and not append(candidate):
                    return probe.finish(data)
        data["face_scope"] = "solid_body_faces"
        return probe.finish(data)

    def _find_feature_readonly(self, document: Any, name: str, probe: _ReadProbe) -> Any:
        try:
            feature = _read_member(document, "FeatureByName", name)
            if feature is not None and _read_member(feature, "Name") == name:
                return feature
        except Exception:
            pass
        feature = probe.get(document, "FirstFeature")
        seen: set[str] = set()
        for _ in range(3000):
            if feature is None:
                return None
            candidate = probe.get(feature, "Name")
            if not isinstance(candidate, str) or candidate in seen:
                probe.issue("feature enumeration is cyclic or ambiguous")
                return None
            seen.add(candidate)
            if candidate == name:
                return feature
            feature = probe.get(feature, "GetNextFeature")
        probe.issue("feature lookup limit reached")
        return None

    def _feature_error_readonly(self, feature: Any) -> int:
        method = getattr(feature, "GetErrorCode2")
        try:
            values = _items(method(self._bool_reference()))
        except Exception:
            try:
                values = _items(method(False))
            except Exception:
                values = _items(method())
        if not values or values[0] is None or isinstance(values[0], bool):
            raise ValueError("feature error code unavailable")
        return int(values[0])

    def _inspect_feature(self, name: str, limit: int = 30) -> dict[str, Any]:
        probe = _ReadProbe()
        document, data = self._investigation_document(probe)
        data.update(requested_name=name[:250], found=False, feature=None, limit=limit, truncated=False)
        if not 1 <= len(name) <= 250:
            probe.issue("invalid feature name")
            return probe.finish(data)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            probe.issue("invalid feature inspection limit")
            return probe.finish(data)
        if document is None:
            return probe.finish(data)
        feature = self._find_feature_readonly(document, name, probe)
        if feature is None:
            return probe.finish(data)
        details: dict[str, Any] = {
            "name": name, "type": probe.get(feature, "GetTypeName2"),
            "error_code": probe.attempt("GetErrorCode2", lambda: self._feature_error_readonly(feature)),
            "persist_reference": self._read_persistent_reference(document, feature, probe),
            "dimensions": [], "planar_faces": [],
            "dimensions_truncated": False, "faces_truncated": False,
        }
        if not isinstance(details["type"], str) or not details["type"]:
            probe.issue("feature type is unavailable")
        data.update(found=True, feature=details)
        display = probe.get(feature, "GetFirstDisplayDimension")
        seen: set[str] = set()
        for _ in range(limit):
            if display is None:
                break
            def parameter() -> dict[str, Any]:
                dimension = _read_member(display, "GetDimension2", 0)
                full_name = _read_member(dimension, "FullName")
                if not isinstance(full_name, str) or not 1 <= len(full_name) <= 1000:
                    raise ValueError("dimension name is unavailable")
                parameter_name = self._local_parameter_name(full_name)
                kind = int(_read_member(dimension, "GetType"))
                value = float(_read_member(dimension, "SystemValue"))
                if (not parameter_name or kind not in {0, 1, 2} or not math.isfinite(value)
                        or (kind == 0 and not math.isfinite(value * 1000))):
                    raise ValueError("dimension type or value is unknown")
                return {"name": parameter_name, "parameter_type": kind, "system_value": value,
                        "value_mm": value * 1000 if kind == 0 else None}
            dimension = probe.attempt("feature dimension", parameter)
            if dimension is not None:
                if dimension["name"] in seen:
                    probe.issue("feature dimension enumeration cycle")
                    break
                seen.add(dimension["name"])
                details["dimensions"].append(dimension)
            display = probe.get(feature, "GetNextDisplayDimension", display)
        else:
            if display is not None:
                details["dimensions_truncated"] = data["truncated"] = True
                probe.issue("feature dimension limit reached; returned dimensions are partial")
        faces_read = probe.attempt("GetFaces", lambda: _items(_read_member(feature, "GetFaces")))
        faces = faces_read or []
        details["face_count"] = len(faces) if faces_read is not None else None
        details["faces_inspected"] = min(len(faces), limit) if faces_read is not None else None
        if len(faces) > limit:
            details["faces_truncated"] = data["truncated"] = True
            probe.issue("feature face limit reached")
        for face in faces[:limit]:
            candidate = self._read_planar_reference(document, face, probe)
            if candidate is not None:
                details["planar_faces"].append(candidate)
        details["dimension_scope"] = "feature_display_dimensions_including_subfeatures"
        return probe.finish(data)

    def _inspect_sketch(self) -> dict[str, Any]:
        probe = _ReadProbe()
        document, data = self._investigation_document(probe)
        data.update(active=None, sketch=None)
        if document is None:
            return probe.finish(data)
        manager = probe.get(document, "SketchManager")
        if manager is None:
            probe.issue("SketchManager is unavailable")
            return probe.finish(data)
        sketch = probe.get(manager, "ActiveSketch") if manager is not None else None
        if sketch is None:
            data["active"] = None if probe.issues else False
            return probe.finish(data)
        feature = probe.attempt("SketchFeature", lambda: self._owning_sketch_feature(document, sketch))
        details: dict[str, Any] = {"name": probe.get(feature, "Name") if feature is not None else None,
                                  "segments": [], "profile_checks": [], "geometry_complete": False}
        details.update(coordinate_space="sketch_local", transform_complete=False,
                       transform_source="ISketch.ModelToSketchTransform.ArrayData")
        transform = probe.attempt("ModelToSketchTransform", lambda: self._strict_vector(
            _read_member(_read_member(sketch, "ModelToSketchTransform"), "ArrayData"), 16))
        details["model_to_sketch_transform"] = transform
        details["transform_complete"] = transform is not None
        data.update(active=True, sketch=details)
        if not isinstance(details["name"], str) or not details["name"]:
            probe.issue("active sketch identity is unavailable")
        elif len(details["name"]) > 250:
            details["name"] = details["name"][:250]
        raw_segments = probe.get(sketch, "GetSketchSegments")
        segments = _items(raw_segments)
        details["entity_count"] = len(segments) if not probe.issues else None
        if len(segments) > 128:
            probe.issue("sketch segment result limit reached")
        for segment in segments[:128]:
            kind = probe.get(segment, "GetType")
            construction = probe.get(segment, "ConstructionGeometry")
            if not isinstance(kind, int) or not isinstance(construction, (bool, int)):
                probe.issue("sketch segment type or construction state is unavailable")
            entry = {"type": kind, "construction": construction}
            if kind == 1:
                arc_geometry = probe.attempt("circle geometry", lambda: self._segment_geometry(segment))
                entry.update(arc_geometry or {"is_circle": None, "radius_mm": None})
            if kind == 0:
                for member, field_name in (("GetStartPoint2", "start_sketch_m"),
                                           ("GetEndPoint2", "end_sketch_m")):
                    def point() -> list[float]:
                        value = _read_member(segment, member)
                        return self._strict_vector([_read_member(value, axis) for axis in ("X", "Y", "Z")], 3)
                    entry[field_name] = probe.attempt(member, point)
            details["segments"].append(entry)
        try:
            geometry, entities = self._capture_edit_geometry(document, sketch)
            details.update({key: value for key, value in geometry.items() if key != "sketch_name"})
            for item in details["segments"]:
                owned = self._owned_circles.get(item["entity_ref"])
                item["editable"] = bool(owned and owned["geometry"] == item
                                        and owned["document"] == self._com_identity(document)
                                        and owned["sketch"] == self._com_identity(sketch)
                                        and owned["entity"] == self._com_identity(entities[item["entity_ref"]]))
        except Exception as exc:
            details["repair_unavailable"] = str(exc)[:300]
        for usage, usage_name in ((3, "boss-extrude"), (8, "boss-revolve")):
            def check() -> dict[str, Any]:
                method = getattr(sketch, "CheckFeatureUse")
                try:
                    opened, closed = self._int_reference(), self._int_reference()
                    values = self._sketch_check_result(method(usage, opened, closed), opened, closed)
                except Exception:
                    values = self._sketch_check_result(method(usage, 0, 0))
                status, open_count, closed_count = values
                if open_count is None or closed_count is None:
                    raise ValueError("sketch contour counts are unavailable")
                return {"usage": usage_name, "status": status, "open_contours": open_count,
                        "closed_contours": closed_count, "usable": status == 0 and closed_count > 0,
                        "status_description": SKETCH_CHECK_STATUS.get(status, "通过" if status == 0 else "未识别状态码")}
            profile = probe.attempt(f"CheckFeatureUse/{usage_name}", check)
            details["profile_checks"].append(profile or {"usage": usage_name, "status": None, "usable": None})
        return probe.finish(data)

    @staticmethod
    def _body_extreme_point(body: Any, direction: tuple[float, float, float]) -> list[float]:
        """Fixed read-only IBody2 method with explicit double out parameters."""
        import pythoncom
        from win32com.client import VARIANT

        outputs = [VARIANT(pythoncom.VT_BYREF | pythoncom.VT_R8, float("nan")) for _ in range(3)]
        ole = body._oleobj_
        found = ole.Invoke(ole.GetIDsOfNames("GetExtremePoint"), 0, pythoncom.DISPATCH_METHOD, True,
                           *direction, *outputs)
        if not (found is True or type(found) is int and found in {1, -1}):
            raise ValueError("GetExtremePoint did not find a point")
        return SolidWorksGateway._strict_vector([output.value for output in outputs], 3)

    def _measure_extents(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "complete": False, "document_identity": None, "document_title": None, "document_path": None,
            "configuration": None, "configuration_identity": None, "coordinate_system": "model",
            "extents_mm": dict.fromkeys(("x", "y", "z")), "source": "IBody2.GetExtremePoint",
            "bodies_count": None, "issues": [],
        }
        try:
            header = self.probe_document_identity()
            data.update(header)
            if not header["connected"] or header["document_type"] != "part":
                raise ValueError("a connected active part document is required")
            document = _read_member(self._sw, "ActiveDoc")
            if self._com_identity(document) != header["document_identity"]:
                raise ValueError("active document changed before measurement")

            def configuration() -> tuple[str, str]:
                config = _read_member(_read_member(document, "ConfigurationManager"), "ActiveConfiguration")
                name = _read_member(config, "Name")
                if not isinstance(name, str) or not 1 <= len(name) <= 250 or "\0" in name:
                    raise ValueError("active configuration is unknown")
                return name, self._com_identity(config)

            config_before = configuration()
            data["configuration"], data["configuration_identity"] = config_before

            def solids() -> list[Any]:
                import pythoncom
                from win32com.client import Dispatch

                ole = document._oleobj_
                raw = ole.Invoke(ole.GetIDsOfNames("GetBodies2"), 0, pythoncom.DISPATCH_METHOD, True, 0, False)
                if raw is None:
                    return []
                if not isinstance(raw, (tuple, list)) or len(raw) > 64:
                    raise ValueError("solid body collection is invalid or exceeds 64 bodies")
                return [Dispatch(body) for body in raw]

            bodies = solids()
            data["bodies_count"] = len(bodies)
            if not bodies:
                raise ValueError("no solid bodies are available for extents measurement")
            identities = {self._com_identity(body) for body in bodies}
            if len(identities) != len(bodies):
                raise ValueError("solid body collection contains duplicate identities")
            minimum = [float("inf")] * 3
            maximum = [float("-inf")] * 3
            for body in bodies:
                for axis, positive in enumerate(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))):
                    negative = (-positive[0], -positive[1], -positive[2])
                    high = self._body_extreme_point(body, positive)[axis]
                    low = self._body_extreme_point(body, negative)[axis]
                    if low > high:
                        raise ValueError("directional extreme coordinates are inconsistent")
                    minimum[axis], maximum[axis] = min(minimum[axis], low), max(maximum[axis], high)
            after_bodies = solids()
            if (len(after_bodies) != len(bodies)
                    or {self._com_identity(body) for body in after_bodies} != identities):
                raise ValueError("solid body identities changed during measurement")
            if configuration() != config_before or self.probe_document_identity() != header:
                raise ValueError("document or configuration changed during measurement")
            extents = [(high - low) * 1000 for low, high in zip(minimum, maximum, strict=True)]
            if not all(math.isfinite(value) and value > 0 for value in extents):
                raise ValueError("solid extents are not finite positive lengths")
            data.update(complete=True, extents_mm=dict(zip(("x", "y", "z"), extents, strict=True)),
                        min_mm=[value * 1000 for value in minimum], max_mm=[value * 1000 for value in maximum])
        except Exception as exc:
            data["issues"] = [f"{type(exc).__name__}: {str(exc)[:350]}"]
        return bound_investigation_result(data)

    def _observe_model(self, include_configuration: bool = False) -> dict[str, Any]:
        """Read evidence without selecting, opening, rebuilding or changing a model.

        Unlike the best-effort UI snapshot, unknown values remain None and every
        failed required read invalidates completeness. No zero-filled fallback
        can therefore satisfy a task acceptance criterion.
        """
        snapshot: dict[str, Any] = ModelSnapshot(connected=self._sw is not None).to_dict()
        dimensions: list[dict[str, Any]] = []
        features: list[dict[str, Any]] = []
        planes: list[dict[str, Any]] = []
        selections: list[dict[str, Any]] = []
        issues: list[str] = []
        result: dict[str, Any] = {
            "snapshot": snapshot, "dimensions": dimensions, "features": features,
            "planes": planes, "complete": True, "issues": issues,
            "selection": selections,
            "document_identity": None,
        }
        if include_configuration:
            result.update(configuration=None, configuration_identity=None)
        if self._sw is None:
            if include_configuration:
                issues.append("Configuration: no connected document")
                result["complete"] = False
            return result

        def read(obj: Any, member: str, *args: Any) -> Any:
            value = getattr(obj, member)
            if hasattr(value, "_oleobj_"):
                return value
            return value(*args) if callable(value) else value

        def attempt(label: str, operation: Any) -> Any:
            try:
                return operation()
            except Exception as exc:
                issues.append(f"{label}: {type(exc).__name__}")
                return None

        document = attempt("ActiveDoc", lambda: read(self._sw, "ActiveDoc"))
        if document is None:
            document_read_failed = bool(issues)
            if include_configuration:
                issues.append("Configuration: no active document")
            result["complete"] = not issues
            if document_read_failed:
                snapshot["connected"] = False
            return result
        # IUnknown identity is stable across COM wrappers within this worker.
        # Absence is explicit; object-wrapper id is not a safe document identity.
        result["document_identity"] = attempt("DocumentIdentity", lambda: self._com_identity(document))
        snapshot["document_title"] = attempt("GetTitle", lambda: str(read(document, "GetTitle")))
        snapshot["document_path"] = attempt("GetPathName", lambda: str(read(document, "GetPathName")))
        doc_type = attempt("GetType", lambda: int(read(document, "GetType")))
        snapshot["document_type"] = {1: "part", 2: "assembly", 3: "drawing"}.get(doc_type, "unknown")
        if doc_type not in {1, 2, 3}:
            issues.append("document_type: unknown")

        def configuration_state() -> tuple[str, str]:
            config = read(read(document, "ConfigurationManager"), "ActiveConfiguration")
            name = read(config, "Name")
            if not isinstance(name, str) or not 1 <= len(name) <= 250 or "\0" in name:
                raise ValueError("configuration name is unknown")
            return name, self._com_identity(config)

        config_before = None
        model_stamp = None
        if include_configuration:
            config_before = attempt("Configuration", configuration_state)
            if config_before is not None:
                result["configuration"], result["configuration_identity"] = config_before
            if hasattr(self, "_candidate_root"):
                model_stamp = attempt("GetUpdateStamp", lambda: read(document, "GetUpdateStamp"))
                if not isinstance(model_stamp, int) or isinstance(model_stamp, bool) or model_stamp < 0:
                    issues.append("GetUpdateStamp: unavailable")
                result["model_update_stamp"] = model_stamp

        def sketch_state() -> tuple[str, int]:
            sketch = read(read(document, "SketchManager"), "ActiveSketch")
            if sketch is None:
                return "", 0
            name = _value(sketch, "Name", "")
            if not name:
                name = read(self._owning_sketch_feature(document, sketch), "Name")
            return str(name or "<active-sketch>"), len(_items(read(sketch, "GetSketchSegments")))

        sketch = attempt("ActiveSketch", sketch_state)
        snapshot["active_sketch"], snapshot["sketch_entity_count"] = sketch or (None, None)
        result["active_sketch_geometry"] = None
        result["active_sketch_geometry_complete"] = False
        if sketch and not sketch[0]:
            result["active_sketch_geometry"] = []
            result["active_sketch_geometry_complete"] = True
        elif sketch:
            try:
                geometry, _ = self._capture_edit_geometry(document, read(read(document, "SketchManager"), "ActiveSketch"))
                result["active_sketch_geometry"] = geometry
                result["active_sketch_geometry_complete"] = True
            except Exception:
                # This additional repair capability never fabricates geometry;
                # old broad observation consumers retain their existing contract.
                pass
        snapshot["selection_count"] = attempt(
            "SelectionManager", lambda: int(read(read(document, "SelectionManager"),
                                                "GetSelectedObjectCount2", -1))
        )
        selection_count = snapshot["selection_count"]
        if selection_count is not None:
            if not 0 <= selection_count <= 256:
                issues.append("selection traversal limit reached")
            else:
                for selected_index in range(1, selection_count + 1):
                    def selected_entity() -> dict[str, Any]:
                        manager = read(document, "SelectionManager")
                        kind = int(read(manager, "GetSelectedObjectType3", selected_index, -1))
                        entity = read(manager, "GetSelectedObject6", selected_index, -1)
                        if entity is None:
                            raise ValueError("selected entity unavailable")
                        name = str(_value(entity, "Name", "") or _value(entity, "GetName", ""))
                        reference = ""
                        try:
                            raw = read(read(document, "Extension"), "GetPersistReference3", entity)
                            payload = self._reference_bytes(raw)
                            if 0 < len(payload) <= 8192:
                                reference = base64.b64encode(payload).decode("ascii")
                        except Exception:
                            pass
                        if not name and not reference:
                            raise ValueError("selected entity has no stable reference or name")
                        return {"type": kind, "name": name, "persist_reference": reference}

                    selected = attempt(f"Selection[{selected_index}]", selected_entity)
                    if selected is not None:
                        selections.append(selected)
        snapshot.update(body_count=None, volume_m3=None, surface_area_m2=None,
                        mass_kg=None, mass_available=False)
        if doc_type == 1:
            def bodies_state() -> tuple[int, float, float]:
                bodies = _items(read(document, "GetBodies2", 0, False))
                if len(bodies) > 10_000:
                    raise ValueError("body traversal limit")
                volume, surface = 0.0, 0.0
                for body in bodies:
                    values = _items(read(body, "GetMassProperties", 1.0))
                    if len(values) < 5:
                        raise ValueError("body properties unavailable")
                    body_volume, body_area = float(values[3]), float(values[4])
                    if not all(math.isfinite(v) and v >= 0 for v in (body_volume, body_area)):
                        raise ValueError("invalid body measurements")
                    volume += body_volume
                    surface += body_area
                return len(bodies), volume, surface

            body_state = attempt("GetBodies2/GetMassProperties", bodies_state)
            if body_state is not None:
                snapshot["body_count"], snapshot["volume_m3"], snapshot["surface_area_m2"] = body_state
        volume = snapshot.get("volume_m3")
        snapshot["volume_mm3"] = volume * 1e9 if volume is not None else None

        def feature_error(feature: Any) -> int:
            method = getattr(feature, "GetErrorCode2")
            # Early-bound and late-bound pywin32 expose the out bool differently.
            try:
                values = _items(method(self._bool_reference()))
            except Exception:
                try:
                    values = _items(method(False))
                except Exception:
                    values = _items(method())
            if not values or values[0] is None:
                raise ValueError("feature error code unavailable")
            return int(values[0])

        feature = attempt("FirstFeature", lambda: read(document, "FirstFeature"))
        seen_features: set[tuple[str, str]] = set()
        seen_dimensions: set[str] = set()
        for _ in range(3_000):
            if feature is None:
                break
            name = attempt("Feature.Name", lambda: str(read(feature, "Name")))
            feature_type = attempt("Feature.GetTypeName2", lambda: str(read(feature, "GetTypeName2")))
            identity = (name or "", feature_type or "")
            if identity in seen_features:
                issues.append("feature traversal cycle or ambiguous name")
                break
            seen_features.add(identity)
            error_code = attempt(f"{name}.GetErrorCode2", lambda: feature_error(feature))
            features.append({"name": name, "type": feature_type, "error_code": error_code})
            if str(feature_type).casefold() == "refplane":
                planes.append({"name": name})
            display = attempt(f"{name}.GetFirstDisplayDimension",
                              lambda: read(feature, "GetFirstDisplayDimension"))
            visited_displays: set[str] = set()
            for _ in range(2_000):
                if display is None:
                    break
                if len(dimensions) >= 4_000:
                    issues.append("dimension traversal limit reached")
                    break
                def read_dimension() -> tuple[str, int, float]:
                    dimension = read(display, "GetDimension2", 0)
                    parameter = self._local_parameter_name(str(read(dimension, "FullName")))
                    # IDimension.GetType uses swDimensionParamType_e:
                    # 0 linear, 1 angular, 2 integer, -1 unknown.
                    dimension_type = int(read(dimension, "GetType"))
                    value = float(read(dimension, "SystemValue"))
                    if not parameter or not math.isfinite(value):
                        raise ValueError("invalid dimension evidence")
                    return parameter, dimension_type, value

                dimension_info = attempt(f"{name}.Dimension", read_dimension)
                if dimension_info is not None:
                    parameter, dimension_type, value = dimension_info
                    if parameter in visited_displays:
                        issues.append("display dimension traversal cycle")
                        break
                    visited_displays.add(parameter)
                    if parameter not in seen_dimensions:
                        seen_dimensions.add(parameter)
                        dimensions.append({"name": parameter,
                                           "value_mm": value * 1000.0 if dimension_type == 0 else None,
                                           "system_value": value, "parameter_type": dimension_type})
                    if dimension_type not in {0, 1, 2}:
                        issues.append(f"{parameter}: unknown dimension type")

                def next_dimension() -> Any:
                    # Feature and drawing-view enumeration are different APIs.
                    # IDisplayDimension.GetNext5 belongs to IView enumeration;
                    # IFeature.GetNextDisplayDimension includes absorbed sketch
                    # dimensions and must receive the preceding display object.
                    return read(feature, "GetNextDisplayDimension", display)

                display = attempt(f"{name}.NextDisplayDimension", next_dimension)
            else:
                issues.append("display dimension traversal limit reached")
            feature = attempt(f"{name}.GetNextFeature", lambda: read(feature, "GetNextFeature"))
        else:
            issues.append("feature traversal limit reached")
        if include_configuration:
            config_after = attempt("ConfigurationAfter", configuration_state)
            if hasattr(self, "_candidate_root"):
                stamp_after = attempt("GetUpdateStampAfter", lambda: read(document, "GetUpdateStamp"))
                if model_stamp is None or stamp_after != model_stamp:
                    issues.append("GetUpdateStamp: model changed during observation")
            active_after = attempt("DocumentIdentityAfter", lambda: self._com_identity(read(self._sw, "ActiveDoc")))
            if (config_before is None or config_after != config_before
                    or active_after != result["document_identity"]):
                issues.append("Configuration: document or active configuration changed during observation")
                result.update(configuration=None, configuration_identity=None)
        snapshot["feature_count"] = len(features) if not issues else None
        snapshot["captured_at"] = utc_now()
        result["complete"] = not issues
        candidate = getattr(self, "_candidate_active", None)
        if (candidate is not None and candidate.header is not None and not issues
                and model_stamp is not None and candidate.header["document_identity"] == result["document_identity"]):
            candidate.last_observed_stamp = model_stamp
        return result

    def snapshot(self) -> ModelSnapshot:
        if self._sw is None:
            return ModelSnapshot(connected=False)
        document = self.document
        if document is None:
            return ModelSnapshot(connected=True)
        return self._snapshot_document(document)

    def _snapshot_document(self, document: Any) -> ModelSnapshot:
        doc_type = int(_value(document, "GetType", 0) or 0)
        doc_type_name = {1: "part", 2: "assembly", 3: "drawing"}.get(doc_type, "unknown")
        active_sketch = _value(document.SketchManager, "ActiveSketch")
        sketch_name = ""
        segment_count = 0
        if active_sketch is not None:
            sketch_name = str(_value(active_sketch, "Name", ""))
            if not sketch_name:
                try:
                    feature = self._owning_sketch_feature(document, active_sketch)
                    sketch_name = str(_read_member(feature, "Name") or "<active-sketch>")
                except Exception:
                    # This best-effort snapshot is not the strict observation
                    # gate. Keep an explicit unknown marker, never invent a name.
                    sketch_name = "<active-sketch>"
            segment_count = len(_items(_value(active_sketch, "GetSketchSegments")))
        feature_count = 0
        feature = _value(document, "FirstFeature")
        for _ in range(10000):
            if feature is None:
                break
            feature_count += 1
            feature = _value(feature, "GetNextFeature")
        body_count = 0
        volume = 0.0
        surface_area = 0.0
        mass = 0.0
        mass_available = False
        if doc_type == 1:
            try:
                bodies = _items(document.GetBodies2(0, True))
                body_count = len(bodies)
                for body in bodies:
                    properties = _items(body.GetMassProperties(1.0))
                    if len(properties) > 3:
                        volume += float(properties[3] or 0.0)
                    if len(properties) > 4:
                        surface_area += float(properties[4] or 0.0)
            except Exception:
                pass
            try:
                mass_property = document.Extension.CreateMassProperty2()
                if mass_property is not None:
                    try:
                        mass_property.UseSystemUnits = True
                    except Exception:
                        pass
                    measured_volume = float(_value(mass_property, "Volume", 0.0) or 0.0)
                    measured_area = float(_value(mass_property, "SurfaceArea", 0.0) or 0.0)
                    mass = float(_value(mass_property, "Mass", 0.0) or 0.0)
                    mass_available = mass > 0.0
                    if measured_volume > 0:
                        volume = measured_volume
                    if measured_area > 0:
                        surface_area = measured_area
            except Exception:
                mass = 0.0
        selection_count = 0
        try:
            selection_count = int(document.SelectionManager.GetSelectedObjectCount2(-1) or 0)
        except Exception:
            pass
        return ModelSnapshot(
            connected=True,
            document_title=str(_value(document, "GetTitle", "") or ""),
            document_path=str(_value(document, "GetPathName", "") or ""),
            document_type=doc_type_name,
            active_sketch=sketch_name,
            sketch_entity_count=segment_count,
            feature_count=feature_count,
            body_count=body_count,
            volume_m3=volume,
            surface_area_m2=surface_area,
            mass_kg=mass,
            mass_available=mass_available,
            selection_count=selection_count,
            captured_at=utc_now(),
        )
