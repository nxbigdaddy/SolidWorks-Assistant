"""Host-only immutable native checkpoint files and atomic JSON manifests.

No caller-supplied filesystem path is deserialized. SHA-256 detects accidental
file corruption; it is not a signature against a malicious local administrator.
Unpublished save/temp files are retained but never listed as accepted checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST = 4_000_000
_MAX_NATIVE_PATH = 255


def validate_native_path(path: Path) -> None:
    # SOLIDWORKS swFileSaveAsNameExceedsMaxPathLength (2048), not the
    # filesystem's long-path policy. Windows API lengths count UTF-16 units.
    length = len(str(path).encode("utf-16-le")) // 2
    if length > _MAX_NATIVE_PATH:
        raise ValueError(f"SolidWorks 原生文件路径过长（{length} > {_MAX_NATIVE_PATH}）；请使用更短的工作区路径。")


def validate_task(task_id: str) -> None:
    if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128 or "\0" in task_id:
        raise ValueError("task id is invalid")


def validate_checkpoint(checkpoint_id: str) -> None:
    if not isinstance(checkpoint_id, str) or _ID.fullmatch(checkpoint_id) is None:
        raise ValueError("checkpoint id is invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("checkpoint manifest contains duplicate keys")
        result[key] = value
    return result


class CheckpointStore:
    def __init__(self, root: Path):
        if not root.is_absolute():
            raise ValueError("checkpoint workspace must be absolute")
        self.root = root.resolve()
        self._safe(self.root)

    def _safe(self, path: Path) -> Path:
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            raise ValueError("checkpoint path escaped the host workspace")
        current = path
        while current.is_relative_to(self.root):
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError("checkpoint reparse points are not allowed")
            if current == self.root:
                break
            current = current.parent
        return path

    def _task_dir(self, task_id: str, *, restore: bool = False) -> Path:
        validate_task(task_id)
        return self._safe(self.root / ("restores" if restore else "checkpoints")
                          / hashlib.sha256(task_id.encode("utf-8")).hexdigest())

    def native_path(self, task_id: str, checkpoint_id: str) -> Path:
        validate_checkpoint(checkpoint_id)
        # Keep a short distinguishable basename as well as a full path. Authority remains the full
        # UUID directory + task-bound manifest, never this display abbreviation.
        path = self._safe(self._task_dir(task_id) / checkpoint_id / f"p-{checkpoint_id[:20]}.sldprt")
        validate_native_path(path)
        return path

    def reserve(self, task_id: str) -> tuple[str, Path]:
        checkpoint_id = uuid.uuid4().hex
        path = self.native_path(task_id, checkpoint_id)
        path.parent.mkdir(parents=True, exist_ok=False)
        self._safe(path)
        return checkpoint_id, path

    def digest(self, path: Path) -> tuple[str, int]:
        self._safe(path)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_nlink != 1:
            raise ValueError("checkpoint native file is empty, linked, or not a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("checkpoint native file changed while hashing")
        self._safe(path)
        return digest.hexdigest(), after.st_size

    def publish(self, task_id: str, checkpoint_id: str, *, parent_id: str | None,
                observation: dict[str, Any], geometry: dict[str, Any],
                header: dict[str, Any], stamp: int) -> dict[str, Any]:
        path = self.native_path(task_id, checkpoint_id)
        if parent_id is not None:
            validate_checkpoint(parent_id)
            self.read(task_id, parent_id)
        digest, size = self.digest(path)
        manifest = {
            "schema": 1, "task_id": task_id, "checkpoint_id": checkpoint_id,
            "parent_id": parent_id, "created_at": datetime.now(UTC).isoformat(),
            "native_sha256": digest, "native_size": size,
            "observation": observation, "geometry": geometry,
            "source_header": header, "source_stamp": stamp,
            "validation": "source_unchanged_across_native_copy_save",
        }
        payload = json.dumps(manifest, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_MANIFEST:
            raise ValueError("checkpoint manifest exceeds local evidence limit")
        destination = self._safe(path.parent / "manifest.json")
        if destination.exists():
            raise ValueError("checkpoint manifest already exists")
        temporary = self._safe(path.parent / f"manifest-{uuid.uuid4().hex}.tmp")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self._safe(destination)
        if destination.exists():
            raise ValueError("checkpoint manifest destination changed")
        os.replace(temporary, destination)
        return manifest

    def read(self, task_id: str, checkpoint_id: str) -> dict[str, Any]:
        path = self.native_path(task_id, checkpoint_id)
        manifest_path = self._safe(path.parent / "manifest.json")
        info = manifest_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("checkpoint manifest is linked or not a regular file")
        if info.st_size > _MAX_MANIFEST:
            raise ValueError("checkpoint manifest exceeds local evidence limit")
        with manifest_path.open("rb") as stream:
            payload = stream.read(_MAX_MANIFEST + 1)
        if len(payload) > _MAX_MANIFEST:
            raise ValueError("checkpoint manifest exceeds local evidence limit")
        manifest = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        fields = {"schema", "task_id", "checkpoint_id", "parent_id", "created_at",
                  "native_sha256", "native_size", "observation", "geometry", "source_header",
                  "source_stamp", "validation"}
        if not isinstance(manifest, dict) or set(manifest) != fields:
            raise ValueError("checkpoint manifest schema is invalid")
        if (type(manifest["schema"]) is not int or manifest["schema"] != 1
                or manifest["task_id"] != task_id or manifest["checkpoint_id"] != checkpoint_id
                or manifest["validation"] != "source_unchanged_across_native_copy_save"):
            raise ValueError("checkpoint manifest ownership or schema differs")
        if manifest["parent_id"] is not None:
            validate_checkpoint(manifest["parent_id"])
            if manifest["parent_id"] == checkpoint_id:
                raise ValueError("checkpoint cannot be its own parent")
        if not isinstance(manifest["created_at"], str):
            raise ValueError("checkpoint timestamp is invalid")
        datetime.fromisoformat(manifest["created_at"])
        if not isinstance(manifest["native_sha256"], str) or not _HASH.fullmatch(manifest["native_sha256"]):
            raise ValueError("checkpoint hash is invalid")
        if type(manifest["native_size"]) is not int or manifest["native_size"] <= 0:
            raise ValueError("checkpoint size is invalid")
        if type(manifest["source_stamp"]) is not int or manifest["source_stamp"] < 0:
            raise ValueError("checkpoint source stamp is invalid")
        if any(not isinstance(manifest[key], dict) for key in ("observation", "geometry", "source_header")):
            raise ValueError("checkpoint evidence is invalid")
        json.dumps(manifest, allow_nan=False)
        digest, size = self.digest(path)
        if (digest, size) != (manifest["native_sha256"], manifest["native_size"]):
            raise ValueError("checkpoint native file hash or size differs")
        return manifest

    def list(self, task_id: str) -> dict[str, Any]:
        directory = self._task_dir(task_id)
        checkpoints: list[dict[str, Any]] = []
        issues: list[dict[str, str]] = []
        if directory.exists():
            entries = list(islice(directory.iterdir(), 4097))
            if len(entries) > 4096:
                raise ValueError("checkpoint directory exceeds the local limit")
            for entry in entries:
                if _ID.fullmatch(entry.name) is None:
                    continue
                try:
                    self._safe(entry)
                    if not (entry / "manifest.json").exists():
                        continue
                    manifest = self.read(task_id, entry.name)
                    checkpoints.append({key: manifest[key] for key in (
                        "checkpoint_id", "parent_id", "created_at", "native_sha256", "native_size")}
                                       | {"integrity": "verified"})
                except (OSError, ValueError, TypeError) as exc:
                    issues.append({"checkpoint_id": entry.name, "reason": str(exc)})
        checkpoints.sort(key=lambda item: (item["created_at"], item["checkpoint_id"]))
        return {"ok": True, "status": "listed", "checkpoints": checkpoints, "issues": issues}

    def make_restore_copy(self, task_id: str, checkpoint_id: str,
                          manifest: dict[str, Any]) -> Path:
        # Recheck immediately before copying, then independently hash the new
        # file. The checkpoint itself is never opened or edited by SolidWorks.
        current = self.read(task_id, checkpoint_id)
        if current != manifest:
            raise ValueError("checkpoint manifest changed before restore")
        token = uuid.uuid4().hex
        directory = self._safe(self._task_dir(task_id, restore=True) / token)
        directory.mkdir(parents=True, exist_ok=False)
        destination = self._safe(directory / f"r-{token[:20]}.sldprt")
        validate_native_path(destination)
        with self.native_path(task_id, checkpoint_id).open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        if self.digest(destination) != (manifest["native_sha256"], manifest["native_size"]):
            raise ValueError("restored native copy hash differs from checkpoint")
        return destination
