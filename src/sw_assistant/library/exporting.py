from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..domain import ToolCall, ToolError
from ..solidworks.client import ToolWorker
from .service import LibraryService


EXPORT_FORMATS = {
    ".sldprt": {".step", ".stp", ".stl", ".iges", ".igs"},
    ".sldasm": {".step", ".stp", ".stl", ".iges", ".igs"},
    ".slddrw": {".pdf", ".dxf", ".dwg"},
}


@dataclass(slots=True)
class ExportResult:
    source: str
    destination: str
    source_sha256: str
    destination_sha256: str
    strategy: str = "export"


class ExportService:
    def __init__(self, worker: ToolWorker) -> None:
        self.worker = worker

    def export(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        cancel: threading.Event | None = None,
        timeout_seconds: int = 600,
    ) -> ExportResult:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve(strict=False)
        allowed = EXPORT_FORMATS.get(source_path.suffix.casefold(), set())
        if destination_path.suffix.casefold() not in allowed:
            formats = "、".join(sorted(allowed)) or "无"
            raise ToolError(f"该文档不支持目标格式；允许格式：{formats}。")
        if destination_path.exists():
            raise ToolError("导出目标已经存在，安全导出不会覆盖。")
        if not destination_path.parent.is_dir():
            raise ToolError("导出目标的上级目录不存在。")
        self._check_cancel(cancel)
        source_hash = LibraryService.file_sha256(source_path, cancel)
        staging = destination_path.with_name(
            f".{destination_path.stem}.sw-export-{uuid.uuid4().hex}{destination_path.suffix}"
        )
        published = False
        try:
            result = self.worker.call(
                ToolCall(
                    "document.export",
                    {"source": str(source_path), "destination": str(staging)},
                    "使用 SolidWorks 官方转换器导出副本",
                ),
                timeout_seconds,
            )
            if not result.ok:
                raise ToolError(result.message or "SolidWorks 导出失败。")
            self._check_cancel(cancel)
            if not staging.is_file() or staging.stat().st_size <= 0:
                raise ToolError("SolidWorks 没有生成有效的导出文件。")
            if LibraryService.file_sha256(source_path, cancel) != source_hash:
                raise ToolError("导出期间源文件发生变化，未发布输出。")
            destination_hash = LibraryService.file_sha256(staging, cancel)
            self._check_cancel(cancel)
            staging.replace(destination_path)
            published = True
            return ExportResult(
                source=str(source_path),
                destination=str(destination_path),
                source_sha256=source_hash,
                destination_sha256=destination_hash,
            )
        finally:
            if not published:
                staging.unlink(missing_ok=True)

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel and cancel.is_set():
            raise InterruptedError("任务已停止。")
