from __future__ import annotations

import hashlib
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import ToolCall, ToolError
from ..solidworks.client import ToolWorker
from .service import LibraryService


@dataclass(slots=True)
class PackAndGoResult:
    source: str
    destination: str
    source_sha256: str
    destination_sha256: str
    strategy: str
    file_count: int
    files: list[str]
    manifest: dict[str, Any] | None = None


class PackAndGoService:
    def __init__(self, worker: ToolWorker) -> None:
        self.worker = worker

    def package_assembly(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        include_drawings: bool = True,
        include_toolbox: bool = True,
        cancel: threading.Event | None = None,
        timeout_seconds: int = 600,
    ) -> PackAndGoResult:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve(strict=False)
        if source_path.suffix.casefold() != ".sldasm":
            raise ToolError("Pack and Go 当前只接受现有的 SLDASM 装配体。")
        if destination_path.exists():
            raise ToolError("Pack and Go 目标目录已经存在，安全发布不会覆盖。")
        if not destination_path.parent.is_dir():
            raise ToolError("Pack and Go 目标目录的上级目录不存在。")
        self._check_cancel(cancel)
        source_hash = LibraryService.file_sha256(source_path, cancel)
        staging = destination_path.with_name(
            f".{destination_path.name}.sw-pack-{uuid.uuid4().hex}"
        )
        staging.mkdir()
        published = False
        try:
            result = self.worker.call(
                ToolCall(
                    "assembly.pack_and_go",
                    {
                        "source": str(source_path),
                        "destination_dir": str(staging),
                        "include_drawings": bool(include_drawings),
                        "include_toolbox": bool(include_toolbox),
                    },
                    "使用官方 Pack and Go 复制装配体及引用",
                ),
                timeout_seconds,
            )
            if not result.ok:
                raise ToolError(result.message or "SolidWorks Pack and Go 失败。")
            self._check_cancel(cancel)
            files = self._validate_staging(staging, cancel)
            if not any(path.suffix.casefold() == ".sldasm" for path in files):
                raise ToolError("Pack and Go 输出中没有装配体文件。")
            if LibraryService.file_sha256(source_path, cancel) != source_hash:
                raise ToolError("Pack and Go 期间源装配体发生变化，未发布输出。")
            package_hash = self._package_hash(staging, files, cancel)
            self._check_cancel(cancel)
            staging.replace(destination_path)
            published = True
            relative_files = [
                str(path.relative_to(staging)).replace("\\", "/") for path in files
            ]
            return PackAndGoResult(
                source=str(source_path),
                destination=str(destination_path),
                source_sha256=source_hash,
                destination_sha256=package_hash,
                strategy="pack_and_go",
                file_count=len(files),
                files=relative_files,
                manifest={
                    "include_drawings": bool(include_drawings),
                    "include_toolbox": bool(include_toolbox),
                    "api_files": list(result.data.get("saved_paths") or []),
                },
            )
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel and cancel.is_set():
            raise InterruptedError("任务已停止。")

    def _validate_staging(
        self, staging: Path, cancel: threading.Event | None
    ) -> list[Path]:
        root = staging.resolve()
        files: list[Path] = []
        for candidate in staging.rglob("*"):
            self._check_cancel(cancel)
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if root not in resolved.parents:
                raise ToolError("Pack and Go 输出路径越过临时目录。")
            if resolved.stat().st_size <= 0:
                raise ToolError(f"Pack and Go 生成了空文件：{resolved.name}")
            files.append(resolved)
        if not files:
            raise ToolError("Pack and Go 没有生成任何文件。")
        return sorted(files, key=lambda value: str(value).casefold())

    def _package_hash(
        self,
        staging: Path,
        files: list[Path],
        cancel: threading.Event | None,
    ) -> str:
        digest = hashlib.sha256()
        for path in files:
            self._check_cancel(cancel)
            relative = str(path.relative_to(staging)).replace("\\", "/")
            digest.update(relative.casefold().encode("utf-8"))
            digest.update(b"\0")
            digest.update(LibraryService.file_sha256(path, cancel).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()
