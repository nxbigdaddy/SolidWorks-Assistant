from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain import ToolError, utc_now


DOCUMENT_TYPES = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return list(value)
    return [value]


def _object_result(value: Any) -> Any:
    for item in _items(value):
        if item is not None and not isinstance(item, (bool, int, float, str)):
            return item
    return None


def _call_first(value: Any, names: tuple[str, ...], *argument_sets: tuple[Any, ...]) -> Any:
    for name in names:
        try:
            method = getattr(value, name)
        except Exception:
            continue
        for arguments in argument_sets or ((),):
            try:
                return method(*arguments)
            except Exception:
                continue
    return None


class DocumentManagerReader:
    """Optional metadata reader backed by the licensed SOLIDWORKS Document Manager API."""

    def __init__(self, license_key: str = "") -> None:
        self.license_key = str(license_key).strip()

    @property
    def configured(self) -> bool:
        return bool(self.license_key)

    def inspect(self, path: str | Path) -> dict[str, Any]:
        file_path = Path(path).resolve(strict=True)
        document_type = DOCUMENT_TYPES.get(file_path.suffix.casefold())
        if document_type is None:
            raise ToolError("Document Manager 不支持该文件类型。")
        if not self.license_key:
            raise ToolError("尚未配置 SolidWorks Document Manager 许可证密钥。")
        initialized = False
        document = None
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            initialized = True
            try:
                factory = win32com.client.Dispatch("SwDocumentMgr.SwDMClassFactory")
            except Exception as exc:
                raise ToolError(
                    "未注册 SolidWorks Document Manager 组件，无法进行无界面读取。"
                ) from exc
            try:
                application = factory.GetApplication(self.license_key)
            except Exception as exc:
                raise ToolError(f"Document Manager 许可证初始化失败：{exc}") from exc
            if application is None:
                raise ToolError("Document Manager 没有返回应用对象，请检查许可证密钥。")
            try:
                opened = application.GetDocument(
                    str(file_path), document_type, True, 0
                )
            except Exception as exc:
                raise ToolError(f"Document Manager 无法打开文件：{exc}") from exc
            document = _object_result(opened) or opened
            if document is None or isinstance(document, (bool, int, float, str, tuple, list)):
                raise ToolError("Document Manager 没有返回文档对象。")
            return self._manifest(document, file_path, document_type)
        finally:
            if document is not None:
                _call_first(document, ("CloseDoc", "Close"), ())
            if initialized:
                try:
                    import pythoncom

                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _manifest(
        self, document: Any, file_path: Path, document_type: int
    ) -> dict[str, Any]:
        properties = self._properties(document)
        configuration_manager = None
        try:
            configuration_manager = document.ConfigurationManager
        except Exception:
            configuration_manager = _call_first(
                document, ("GetConfigurationManager",), ()
            )
        configuration_names = [
            str(value)
            for value in _items(
                _call_first(
                    configuration_manager,
                    ("GetConfigurationNames", "GetConfigurationNames2"),
                    (),
                )
            )
            if value is not None
        ]
        configuration_properties: dict[str, dict[str, str]] = {}
        for name in configuration_names:
            configuration = _call_first(
                configuration_manager,
                ("GetConfigurationByName", "GetConfiguration"),
                (name,),
            )
            configuration_properties[name] = self._properties(configuration)
        references = self._references(document)
        return {
            "schema_version": 2,
            "reader": "document_manager",
            "completeness": "metadata",
            "path": str(file_path),
            "title": file_path.name,
            "document_type": {1: "part", 2: "assembly", 3: "drawing"}[document_type],
            "configurations": configuration_names,
            "features": [],
            "dimensions": [],
            "custom_properties": properties,
            "configuration_properties": configuration_properties,
            "material": {"name": "", "database": ""},
            "equations": [],
            "references": references,
            "drawing": {"sheets": [], "views": []},
            "geometry": {},
            "captured_at": utc_now(),
        }

    @staticmethod
    def _properties(value: Any) -> dict[str, str]:
        if value is None:
            return {}
        names = _items(
            _call_first(
                value,
                ("GetCustomPropertyNames", "GetCustomPropertyNames2"),
                (),
            )
        )
        properties: dict[str, str] = {}
        for raw_name in names:
            name = str(raw_name)
            result = _call_first(
                value,
                ("GetCustomProperty2", "GetCustomProperty"),
                (name,),
            )
            values = [item for item in _items(result) if isinstance(item, (str, int, float))]
            if values:
                properties[name] = str(values[-1])
        return properties

    @staticmethod
    def _references(document: Any) -> list[dict[str, Any]]:
        result = _call_first(
            document,
            (
                "GetAllExternalReferences5",
                "GetAllExternalReferences4",
                "GetAllExternalReferences3",
                "GetAllExternalReferences2",
                "GetAllExternalReferences",
            ),
            (),
            (0,),
        )
        paths: list[str] = []
        for item in _items(result):
            if isinstance(item, str) and item:
                paths.append(item)
            elif isinstance(item, (tuple, list)):
                paths.extend(str(value) for value in item if isinstance(value, str) and value)
        return [{"path": path} for path in dict.fromkeys(paths)]
