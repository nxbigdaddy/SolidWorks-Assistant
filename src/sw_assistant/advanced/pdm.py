from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import CapabilityStatus
from .models import CapabilityState


@dataclass(frozen=True, slots=True)
class PdmDocumentInfo:
    path: str
    vault: str = ""
    file_id: str = ""
    version: str = ""
    workflow_state: str = ""
    checked_out_by: str = ""
    local_version_current: bool | None = None
    properties: Mapping[str, str] = field(default_factory=dict)
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "vault": self.vault,
            "file_id": self.file_id,
            "version": self.version,
            "workflow_state": self.workflow_state,
            "checked_out_by": self.checked_out_by,
            "local_version_current": self.local_version_current,
            "properties": dict(self.properties),
            "references": list(self.references),
        }


class PdmReadAdapter(Protocol):
    def capability(self, capability_id: str) -> CapabilityStatus: ...

    def inspect(self, path: str, *, cancel: threading.Event) -> PdmDocumentInfo: ...


class PdmIntegrationService:
    """Read-only PDM boundary; mutation intentionally remains outside this service."""

    def __init__(self, adapter: PdmReadAdapter) -> None:
        self.adapter = adapter

    def inspect(
        self, path: str, *, cancel: threading.Event | None = None
    ) -> PdmDocumentInfo:
        source = Path(path).resolve(strict=False)
        if source.suffix.casefold() not in {".sldprt", ".sldasm", ".slddrw"}:
            raise ValueError("PDM 查询只接受 SolidWorks 文档。")
        capability = self.adapter.capability("pdm.read")
        if not capability.available:
            raise RuntimeError(f"PDM 读取不可用：{capability.reason}")
        cancellation = cancel or threading.Event()
        if cancellation.is_set():
            raise InterruptedError("PDM 查询已取消。")
        result = self.adapter.inspect(str(source), cancel=cancellation)
        if Path(result.path).resolve(strict=False) != source:
            raise RuntimeError("PDM 适配器返回了错误的文件。")
        return result


class SolidWorksPdmReadAdapter:
    """Read the current PDM Professional session without logging in or mutating it."""

    def capability(self, capability_id: str) -> CapabilityStatus:
        if capability_id != "pdm.read":
            return CapabilityStatus(
                capability_id,
                CapabilityState.UNKNOWN,
                "PDM 适配器不认识该能力。",
                source="pdm-professional",
            )
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"ConisioLib.EdmVault"):
                pass
        except (ImportError, FileNotFoundError, OSError):
            return CapabilityStatus(
                capability_id,
                CapabilityState.UNAVAILABLE,
                "本机没有注册 SOLIDWORKS PDM Professional COM 组件。",
                source="pdm-professional",
            )
        return CapabilityStatus(
            capability_id,
            CapabilityState.AVAILABLE,
            "PDM Professional COM 组件已注册；仅使用现有登录会话进行只读查询。",
            source="pdm-professional",
        )

    def inspect(self, path: str, *, cancel: threading.Event) -> PdmDocumentInfo:
        if cancel.is_set():
            raise InterruptedError("PDM 查询已取消。")
        source = Path(path).resolve(strict=False)
        initialized = False
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            initialized = True
            vault = win32com.client.Dispatch("ConisioLib.EdmVault")
            if not bool(_read(vault, "IsLoggedIn", False)):
                raise RuntimeError("PDM 当前没有已登录的 Windows 会话；助手不会自动登录。")
            result = vault.GetFileFromPath(str(source), None)
            file_object = _object_result(result)
            if file_object is None:
                raise RuntimeError("该文件不属于当前已登录的 PDM 库，或本地视图不可用。")
            folder = _second_object(result)
            vault_name = str(
                _read(vault, "Name", "")
                or _read(vault, "VaultName", "")
                or _read(vault, "RootFolderPath", "")
            )
            checked_out_by = str(
                _read(file_object, "LockedByUserName", "")
                or _read(file_object, "LockPath", "")
            )
            references: list[str] = []
            try:
                reference_tree = file_object.GetReferenceTree(
                    int(_read(folder, "ID", 0) or 0), 0
                )
                position = reference_tree.GetFirstChildPosition3(0, False, True, 0)
                for _ in range(10000):
                    if not position:
                        break
                    child = reference_tree.GetNextChild(position)
                    reference_path = str(_read(child, "FoundPath", "") or "")
                    if reference_path:
                        references.append(reference_path)
            except Exception:
                pass
            return PdmDocumentInfo(
                path=str(source),
                vault=vault_name,
                file_id=str(_read(file_object, "ID", "") or ""),
                version=str(_read(file_object, "CurrentVersion", "") or ""),
                workflow_state=str(_read(file_object, "CurrentStateName", "") or ""),
                checked_out_by=checked_out_by,
                local_version_current=_optional_bool(
                    _read(file_object, "IsCurrentVersion", None)
                ),
                references=tuple(dict.fromkeys(references)),
            )
        finally:
            if initialized:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass


def _read(value: Any, name: str, default: Any = None) -> Any:
    try:
        member = getattr(value, name)
        return member() if callable(member) else member
    except Exception:
        return default


def _object_result(value: Any) -> Any:
    values = value if isinstance(value, (tuple, list)) else (value,)
    for item in values:
        if item is not None and not isinstance(item, (bool, int, float, str)):
            return item
    return None


def _second_object(value: Any) -> Any:
    values = value if isinstance(value, (tuple, list)) else (value,)
    objects = [
        item
        for item in values
        if item is not None and not isinstance(item, (bool, int, float, str))
    ]
    return objects[1] if len(objects) > 1 else None


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if isinstance(value, (bool, int)) else None
