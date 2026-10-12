from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..domain import ToolCall
from ..solidworks.client import ToolWorker
from .document_manager import DocumentManagerReader
from .repository import DocumentRecord, LibraryRepository


CAD_TYPES = {".sldprt": "part", ".sldasm": "assembly", ".slddrw": "drawing"}


@dataclass(slots=True)
class LibraryProgress:
    stage: str
    total: int
    processed: int
    succeeded: int
    failed: int
    skipped: int
    current_path: str
    elapsed_seconds: float
    message: str


ProgressCallback = Callable[[LibraryProgress], None]


class LibraryService:
    def __init__(self, repository: LibraryRepository, worker: ToolWorker) -> None:
        self.repository = repository
        self.worker = worker

    @staticmethod
    def quick_fingerprint(path: Path, block_size: int = 65536) -> str:
        stat = path.stat()
        digest = hashlib.sha256()
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        with path.open("rb") as stream:
            digest.update(stream.read(block_size))
            if stat.st_size > block_size:
                stream.seek(max(0, stat.st_size - block_size))
                digest.update(stream.read(block_size))
        return digest.hexdigest()

    @staticmethod
    def file_sha256(path: Path, cancel: threading.Event | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                if cancel and cancel.is_set():
                    raise InterruptedError("任务已停止。")
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def manifest_sha256(manifest: dict) -> str:
        canonical = dict(manifest)
        for volatile in ("captured_at", "path", "title"):
            canonical.pop(volatile, None)
        encoded = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def scan(
        self,
        root: str | Path,
        *,
        recursive: bool = True,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, int | bool]:
        base = Path(root).resolve()
        if not base.is_dir():
            raise FileNotFoundError(f"目录不存在：{base}")
        root_id = self.repository.add_root(str(base), recursive)
        started = time.monotonic()
        files: list[Path] = []
        discovery_errors: list[str] = []

        def candidates():
            if not recursive:
                try:
                    entries = list(base.iterdir())
                except OSError as exc:
                    discovery_errors.append(str(exc))
                    return
                yield from entries
                return

            def on_error(error: OSError) -> None:
                discovery_errors.append(str(error))

            for directory, _subdirectories, filenames in os.walk(
                base, topdown=True, onerror=on_error, followlinks=False
            ):
                for filename in filenames:
                    yield Path(directory) / filename

        for candidate in candidates():
            if cancel and cancel.is_set():
                return {
                    "total": len(files), "processed": 0, "succeeded": 0,
                    "failed": len(discovery_errors), "skipped": 0, "cancelled": True,
                }
            if candidate.suffix.casefold() in CAD_TYPES:
                files.append(candidate)
                if progress and len(files) % 100 == 0:
                    progress(
                        LibraryProgress(
                            "discovering", 0, len(files), 0, len(discovery_errors), 0,
                            str(candidate),
                            round(time.monotonic() - started, 2),
                            f"已发现 {len(files)} 个 SolidWorks 文件",
                        )
                    )
        seen: set[str] = set()
        succeeded = skipped = 0
        failed = len(discovery_errors)
        for index, path in enumerate(files, 1):
            if cancel and cancel.is_set():
                return {
                    "total": len(files), "processed": index - 1, "succeeded": succeeded,
                    "failed": failed, "skipped": skipped, "cancelled": True,
                }
            seen.add(str(path))
            try:
                stat = path.stat()
                fingerprint = self.quick_fingerprint(path)
                _, event = self.repository.upsert_document(
                    root_id=root_id,
                    path=str(path),
                    document_type=CAD_TYPES[path.suffix.casefold()],
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    quick_fingerprint=fingerprint,
                )
                if event == "unchanged":
                    skipped += 1
                    message = "文件未变化，保留现有索引"
                else:
                    succeeded += 1
                    message = "已更新文件索引"
            except Exception as exc:
                failed += 1
                message = str(exc)
            if progress:
                progress(
                    LibraryProgress(
                        "scanning", len(files), index, succeeded, failed, skipped, str(path),
                        round(time.monotonic() - started, 2), message,
                    )
                )
        if not discovery_errors:
            self.repository.mark_missing(root_id, seen)
        return {
            "total": len(files), "processed": len(files), "succeeded": succeeded,
            "failed": failed, "skipped": skipped, "cancelled": False,
            "incomplete": bool(discovery_errors),
        }

    def deep_read(
        self,
        records: list[DocumentRecord],
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, int | bool]:
        started = time.monotonic()
        succeeded = failed = skipped = 0
        for index, record in enumerate(records, 1):
            if cancel and cancel.is_set():
                return {
                    "total": len(records), "processed": index - 1, "succeeded": succeeded,
                    "failed": failed, "skipped": skipped, "cancelled": True,
                }
            path = Path(record.path)
            try:
                file_hash = self.file_sha256(path, cancel)
                unchanged = (
                    record.parameter_status == "ready"
                    and bool(record.file_sha256 and record.manifest_sha256)
                    and file_hash == record.file_sha256
                )
                if unchanged:
                    self._refresh_file_metadata(record.id, path)
                    skipped += 1
                    message = "文件与参数快照未变化，已跳过"
                    if progress:
                        progress(
                            LibraryProgress(
                                "deep_read", len(records), index, succeeded, failed, skipped,
                                record.path, round(time.monotonic() - started, 2), message,
                            )
                        )
                    continue
                result = self.worker.call(
                    ToolCall("library.inspect_document", {"path": str(path)}), 300
                )
                if not result.ok:
                    raise RuntimeError(result.message)
                manifest = dict(result.data["manifest"])
                verified_hash = self.file_sha256(path, cancel)
                if verified_hash != file_hash:
                    raise RuntimeError("文件在参数读取期间发生变化，请重试。")
                parameter_hash = self.manifest_sha256(manifest)
                self._refresh_file_metadata(record.id, path)
                self.repository.set_manifest(record.id, manifest, parameter_hash, file_hash)
                succeeded += 1
                message = f"参数哈希 {parameter_hash[:12]}"
            except InterruptedError:
                return {
                    "total": len(records), "processed": index - 1,
                    "succeeded": succeeded, "failed": failed, "skipped": skipped,
                    "cancelled": True,
                }
            except Exception as exc:
                if cancel and cancel.is_set():
                    return {
                        "total": len(records), "processed": index - 1,
                        "succeeded": succeeded, "failed": failed, "skipped": skipped,
                        "cancelled": True,
                    }
                self.repository.mark_read_failed(record.id, str(exc))
                failed += 1
                message = str(exc)
            if progress:
                progress(
                    LibraryProgress(
                        "deep_read", len(records), index, succeeded, failed, skipped, record.path,
                        round(time.monotonic() - started, 2), message,
                    )
                )
        return {
            "total": len(records), "processed": len(records), "succeeded": succeeded,
            "failed": failed, "skipped": skipped, "cancelled": False,
        }

    def metadata_read(
        self,
        records: list[DocumentRecord],
        reader: DocumentManagerReader,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, int | bool]:
        if not reader.configured:
            raise RuntimeError("尚未配置 SolidWorks Document Manager 许可证密钥。")
        started = time.monotonic()
        succeeded = failed = skipped = 0
        for index, record in enumerate(records, 1):
            if cancel and cancel.is_set():
                return {
                    "total": len(records), "processed": index - 1,
                    "succeeded": succeeded, "failed": failed, "skipped": skipped,
                    "cancelled": True,
                }
            path = Path(record.path)
            try:
                file_hash = self.file_sha256(path, cancel)
                unchanged = (
                    record.parameter_status in {"metadata_ready", "ready"}
                    and bool(record.file_sha256 and record.manifest_sha256)
                    and file_hash == record.file_sha256
                )
                if unchanged:
                    self._refresh_file_metadata(record.id, path)
                    skipped += 1
                    message = "文件内容未变化，保留现有参数快照"
                else:
                    manifest = reader.inspect(path)
                    verified_hash = self.file_sha256(path, cancel)
                    if verified_hash != file_hash:
                        raise RuntimeError("文件在无界面属性读取期间发生变化，请重试。")
                    parameter_hash = self.manifest_sha256(manifest)
                    self._refresh_file_metadata(record.id, path)
                    self.repository.set_manifest(
                        record.id,
                        manifest,
                        parameter_hash,
                        file_hash,
                        parameter_status="metadata_ready",
                    )
                    succeeded += 1
                    message = f"无界面属性哈希 {parameter_hash[:12]}"
            except InterruptedError:
                return {
                    "total": len(records), "processed": index - 1,
                    "succeeded": succeeded, "failed": failed, "skipped": skipped,
                    "cancelled": True,
                }
            except Exception as exc:
                if cancel and cancel.is_set():
                    return {
                        "total": len(records), "processed": index - 1,
                        "succeeded": succeeded, "failed": failed, "skipped": skipped,
                        "cancelled": True,
                    }
                self.repository.mark_read_failed(record.id, str(exc))
                failed += 1
                message = str(exc)
            if progress:
                progress(
                    LibraryProgress(
                        "metadata_read", len(records), index, succeeded, failed, skipped,
                        record.path, round(time.monotonic() - started, 2), message,
                    )
                )
        return {
            "total": len(records), "processed": len(records), "succeeded": succeeded,
            "failed": failed, "skipped": skipped, "cancelled": False,
        }

    def _refresh_file_metadata(self, document_id: int, path: Path) -> None:
        stat = path.stat()
        self.repository.update_file_metadata(
            document_id,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            quick_fingerprint=self.quick_fingerprint(path),
        )
