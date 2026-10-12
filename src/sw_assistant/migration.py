from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .infrastructure import AppSettings, HistoryStore, ModelSettings
from .library import LibraryRepository


@dataclass(slots=True)
class MigrationReport:
    imported_documents: int = 0
    imported_manifests: int = 0
    imported_tags: int = 0
    imported_history: int = 0
    warnings: list[str] = field(default_factory=list)


class LegacyMigrator:
    """Read-only importer. It never mutates files below old/."""

    def __init__(self, archive_root: str | Path) -> None:
        self.archive_root = Path(archive_root).resolve()

    def model_settings(self) -> AppSettings | None:
        path = self.archive_root / "config.db"
        if not path.is_file():
            return None
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
                selected = db.execute(
                    "SELECT value FROM settings WHERE key='provider'"
                ).fetchone()
                if not selected:
                    return None
                row = db.execute(
                    "SELECT endpoint,api_key,model FROM providers WHERE key=?", (selected[0],)
                ).fetchone()
            if not row:
                return None
            return AppSettings(
                model=ModelSettings(
                    api_base=str(row[0] or "").strip(),
                    model=str(row[2] or "").strip(),
                    api_key="",
                    credential_unreadable=bool(row[1]),
                )
            )
        except sqlite3.Error:
            return None

    def import_library(self, destination: LibraryRepository) -> MigrationReport:
        report = MigrationReport()
        path = self.archive_root / "model_library.db"
        if not path.is_file():
            report.warnings.append("旧模型库数据库不存在。")
            return report
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            scans = source.execute(
                "SELECT root_path,recursive FROM library_scans ORDER BY id"
            ).fetchall()
            root_ids: list[tuple[Path, int]] = []
            for scan in scans:
                root = Path(str(scan["root_path"]))
                try:
                    root_id = destination.add_root(str(root), bool(scan["recursive"]))
                    root_ids.append((root, root_id))
                except Exception as exc:
                    report.warnings.append(f"无法导入目录 {root}: {exc}")

            files = source.execute("SELECT * FROM library_files ORDER BY id").fetchall()
            id_map: dict[int, int] = {}
            for row in files:
                file_path = Path(str(row["path"]))
                matched_root_id = self._root_for(file_path, root_ids)
                if matched_root_id is None:
                    matched_root_id = destination.add_root(str(file_path.parent), False)
                try:
                    new_id, _ = destination.upsert_document(
                        root_id=matched_root_id,
                        path=str(file_path),
                        document_type=str(row["document_type"]),
                        size_bytes=int(row["size_bytes"]),
                        modified_ns=int(row["modified_ns"]),
                        quick_fingerprint=str(row["quick_fingerprint"]),
                    )
                    id_map[int(row["id"])] = new_id
                    if bool(row["is_missing"]):
                        with destination.connection() as db:
                            db.execute("UPDATE documents SET missing=1 WHERE id=?", (new_id,))
                    report.imported_documents += 1
                except Exception as exc:
                    report.warnings.append(f"无法导入 {file_path}: {exc}")

            snapshots = source.execute(
                "SELECT ps.* FROM parameter_snapshots ps JOIN "
                "(SELECT file_id,MAX(version) version FROM parameter_snapshots GROUP BY file_id) latest "
                "ON latest.file_id=ps.file_id AND latest.version=ps.version"
            ).fetchall()
            fingerprints = {
                int(row["file_id"]): str(row["file_fingerprint"] or "")
                for row in source.execute("SELECT file_id,file_fingerprint FROM parameter_index_state")
            }
            for row in snapshots:
                snapshot_document_id = id_map.get(int(row["file_id"]))
                if snapshot_document_id is None:
                    continue
                try:
                    manifest = json.loads(str(row["manifest_json"]))
                    destination.set_manifest(
                        snapshot_document_id,
                        manifest,
                        str(row["manifest_hash"]),
                        fingerprints.get(int(row["file_id"]), ""),
                    )
                    report.imported_manifests += 1
                except Exception as exc:
                    report.warnings.append(f"参数快照 {row['id']} 导入失败：{exc}")

            tags: dict[int, list[str]] = {}
            for row in source.execute(
                "SELECT ft.file_id,t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
            ):
                tags.setdefault(int(row["file_id"]), []).append(str(row["name"]))
            for old_id, names in tags.items():
                if old_id in id_map:
                    destination.set_tags(id_map[old_id], names)
                    report.imported_tags += len(names)
        return report

    def import_history(self, destination: HistoryStore) -> int:
        path = self.archive_root / "experience.db"
        if not path.is_file():
            return 0
        imported = 0
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            errors = {
                int(row["run_id"]): str(row["error_message"] or "")
                for row in source.execute(
                    "SELECT ce.run_id,ce.error_message FROM command_events ce JOIN "
                    "(SELECT run_id,MAX(id) id FROM command_events "
                    "WHERE status='failed' GROUP BY run_id) latest ON latest.id=ce.id"
                )
            }
            for row in source.execute("SELECT * FROM execution_runs ORDER BY id"):
                raw_state = str(row["result"] or "").casefold()
                state = {
                    "success": "completed",
                    "completed": "completed",
                    "failed": "failed",
                    "error": "failed",
                    "cancelled": "aborted",
                    "aborted": "aborted",
                    "running": "interrupted",
                }.get(raw_state, raw_state or "interrupted")
                if destination.import_legacy(
                    legacy_id=int(row["id"]),
                    prompt=str(row["request_text"] or ""),
                    design_plan=str(row["design_plan"] or ""),
                    state=state,
                    error=errors.get(int(row["id"]), ""),
                    created_at=str(row["started_at"] or ""),
                    updated_at=str(row["finished_at"] or row["started_at"] or ""),
                ):
                    imported += 1
        return imported

    @staticmethod
    def _root_for(path: Path, roots: list[tuple[Path, int]]) -> int | None:
        candidates: list[tuple[int, int]] = []
        folded = str(path).casefold()
        for root, root_id in roots:
            text = str(root).rstrip("\\/")
            if folded == text.casefold() or folded.startswith(text.casefold() + "\\"):
                candidates.append((len(text), root_id))
        return max(candidates)[1] if candidates else None
