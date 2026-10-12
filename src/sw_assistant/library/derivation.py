from __future__ import annotations

import hashlib
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import ToolCall, ToolError
from ..solidworks.client import ToolWorker


@dataclass(slots=True)
class DerivationResult:
    source: str
    destination: str
    source_sha256: str
    destination_sha256: str
    strategy: str
    manifest: dict[str, Any] | None = None


class DerivationService:
    """Create verified part copies without ever writing to the source document."""

    def __init__(self, worker: ToolWorker) -> None:
        self.worker = worker

    @staticmethod
    def file_sha256(path: Path, cancel: threading.Event | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                DerivationService._raise_if_cancelled(cancel)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _raise_if_cancelled(cancel: threading.Event | None) -> None:
        if cancel and cancel.is_set():
            raise InterruptedError("任务已停止。")

    @staticmethod
    def _copy_exclusive(
        source: Path,
        destination: Path,
        cancel: threading.Event | None,
    ) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with source.open("rb") as input_stream, os.fdopen(
                descriptor, "wb"
            ) as output_stream:
                while True:
                    DerivationService._raise_if_cancelled(cancel)
                    block = input_stream.read(1024 * 1024)
                    if not block:
                        break
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            DerivationService._raise_if_cancelled(cancel)
            shutil.copystat(source, destination)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _paths(source: str | Path, destination: str | Path) -> tuple[Path, Path]:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve(strict=False)
        if source_path.suffix.casefold() != ".sldprt":
            raise ToolError("当前安全派生仅支持零件；装配体必须使用 Pack and Go。")
        if destination_path.suffix.casefold() != ".sldprt":
            raise ToolError("目标文件扩展名必须是 .SLDPRT。")
        if destination_path.exists():
            raise ToolError("目标文件已存在，安全派生不会覆盖文件。")
        if not destination_path.parent.is_dir():
            raise ToolError("目标目录不存在。")
        if source_path == destination_path:
            raise ToolError("源文件和目标文件不能相同。")
        return source_path, destination_path

    def copy_part(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        cancel: threading.Event | None = None,
    ) -> DerivationResult:
        source_path, destination_path = self._paths(source, destination)
        source_hash = self.file_sha256(source_path, cancel)
        created = False
        try:
            self._copy_exclusive(source_path, destination_path, cancel)
            created = True
            if self.file_sha256(source_path, cancel) != source_hash:
                raise ToolError("复制期间源文件发生变化，目标副本已取消。")
            destination_hash = self.file_sha256(destination_path, cancel)
            if destination_hash != source_hash:
                raise ToolError("复制后的文件哈希与源文件不一致。")
            return DerivationResult(
                str(source_path),
                str(destination_path),
                source_hash,
                destination_hash,
                "byte_copy",
            )
        except Exception:
            if created:
                destination_path.unlink(missing_ok=True)
            raise

    def derive_part(
        self,
        source: str | Path,
        destination: str | Path,
        changes_mm: dict[str, Any],
        *,
        timeout_seconds: int = 300,
        cancel: threading.Event | None = None,
    ) -> DerivationResult:
        source_path, destination_path = self._paths(source, destination)
        normalized = self._validate_changes(changes_mm)
        source_hash = self.file_sha256(source_path, cancel)
        staging = destination_path.with_name(
            f".{destination_path.stem}.sw-assistant-{uuid.uuid4().hex}{destination_path.suffix}"
        )
        try:
            self._copy_exclusive(source_path, staging, cancel)
            self._raise_if_cancelled(cancel)
            result = self.worker.call(
                ToolCall(
                    "library.modify_part_copy",
                    {"path": str(staging), "changes_mm": normalized},
                    "修改临时零件副本参数",
                ),
                timeout_seconds,
            )
            if not result.ok:
                raise ToolError(result.message or "SolidWorks 参数派生失败。")
            self._raise_if_cancelled(cancel)
            manifest = dict(result.data.get("manifest") or {})
            if not manifest:
                raise ToolError("SolidWorks 没有返回参数派生后的验证 Manifest。")
            if self.file_sha256(source_path, cancel) != source_hash:
                raise ToolError("派生期间源文件发生变化，未发布目标文件。")
            destination_hash = self.file_sha256(staging, cancel)
            self._raise_if_cancelled(cancel)
            os.link(staging, destination_path)
            return DerivationResult(
                str(source_path),
                str(destination_path),
                source_hash,
                destination_hash,
                "parameter_copy",
                manifest,
            )
        finally:
            staging.unlink(missing_ok=True)

    @staticmethod
    def _validate_changes(changes: dict[str, Any]) -> dict[str, float]:
        if not isinstance(changes, dict) or not 1 <= len(changes) <= 500:
            raise ToolError("参数修改数量必须位于 1 到 500 之间。")
        normalized: dict[str, float] = {}
        for raw_name, raw_value in changes.items():
            name = str(raw_name).strip()
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ToolError(f"参数 {name or '<空名称>'} 不是有效毫米数值。") from exc
            if not name or not (-1_000_000 <= value <= 1_000_000):
                raise ToolError(f"参数 {name or '<空名称>'} 超出安全范围。")
            normalized[name] = value
        return normalized
