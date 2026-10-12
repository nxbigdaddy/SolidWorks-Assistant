from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..domain import utc_now


SCHEMA_VERSION = 3


@dataclass(slots=True)
class DocumentRecord:
    id: int
    root_id: int | None
    path: str
    name: str
    document_type: str
    size_bytes: int
    modified_ns: int
    quick_fingerprint: str
    file_sha256: str
    manifest_sha256: str
    parameter_status: str
    missing: bool
    updated_at: str


@dataclass(slots=True)
class SelectionReference:
    id: int
    document_path: str
    document_title: str
    alias: str
    persistent_ref: str
    selection_type: int
    object_name: str
    created_at: str
    updated_at: str


class LibraryRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_state = threading.local()
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = getattr(self._thread_state, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._thread_state.connection = connection
        depth = int(getattr(self._thread_state, "depth", 0))
        self._thread_state.depth = depth + 1
        try:
            yield connection
            if depth == 0:
                connection.commit()
        except Exception:
            if depth == 0:
                connection.rollback()
            raise
        finally:
            self._thread_state.depth = depth

    def close_thread_connection(self) -> None:
        connection = getattr(self._thread_state, "connection", None)
        if connection is not None:
            connection.close()
            del self._thread_state.connection
        self._thread_state.depth = 0

    def _initialize(self) -> None:
        with self.connection() as db:
            # WAL is persistent database metadata; setting it for every short
            # job-item transaction made 1000-item queues needlessly slow.
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_roots (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    recursive INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_scan_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    root_id INTEGER REFERENCES library_roots(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    quick_fingerprint TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL DEFAULT '',
                    manifest_sha256 TEXT NOT NULL DEFAULT '',
                    parameter_status TEXT NOT NULL DEFAULT 'not_read',
                    missing INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_name ON documents(name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type, missing);
                CREATE TABLE IF NOT EXISTS manifest_snapshots (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    manifest_sha256 TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, manifest_sha256)
                );
                CREATE TABLE IF NOT EXISTS document_events (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_tags (
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY(document_id, tag_id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    succeeded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT NOT NULL DEFAULT '',
                    resume_after TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_items (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id,idempotency_key),
                    UNIQUE(job_id,sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_job_items_job_state
                    ON job_items(job_id,state,sequence);
                CREATE TABLE IF NOT EXISTS semantic_documents (
                    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    search_text TEXT NOT NULL,
                    field_tokens_json TEXT NOT NULL,
                    index_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demonstration_sessions (
                    id TEXT PRIMARY KEY,
                    document_type TEXT NOT NULL,
                    document_path TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demonstration_actions (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES demonstration_sessions(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    command_id INTEGER NOT NULL,
                    command_name TEXT NOT NULL DEFAULT '',
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    constraints_json TEXT NOT NULL DEFAULT '{}',
                    steps_json TEXT NOT NULL,
                    risk TEXT NOT NULL DEFAULT 'review',
                    review_state TEXT NOT NULL DEFAULT 'draft',
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_runs (
                    id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    solidworks_revision TEXT NOT NULL DEFAULT '',
                    interop_version TEXT NOT NULL DEFAULT '',
                    capability_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_capability_runs_lookup
                    ON capability_runs(capability_id,solidworks_revision,created_at);
                CREATE TABLE IF NOT EXISTS selection_references (
                    id INTEGER PRIMARY KEY,
                    document_path TEXT NOT NULL COLLATE NOCASE,
                    document_title TEXT NOT NULL,
                    alias TEXT NOT NULL COLLATE NOCASE,
                    persistent_ref TEXT NOT NULL,
                    selection_type INTEGER NOT NULL,
                    object_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(document_path, alias)
                );
                CREATE INDEX IF NOT EXISTS idx_selection_references_document
                    ON selection_references(document_path COLLATE NOCASE, alias COLLATE NOCASE);
                """
            )
            row = db.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row is None:
                db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                current = int(row[0])
                if current > SCHEMA_VERSION or current < 1:
                    raise RuntimeError(f"不支持的模型库版本：{row[0]}")
                if current < 3:
                    self._migrate_jobs_to_v3(db)
                    db.execute("UPDATE schema_info SET version=?", (SCHEMA_VERSION,))
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_state_lease "
                "ON jobs(state,lease_expires_at,created_at)"
            )

    @staticmethod
    def _migrate_jobs_to_v3(db: sqlite3.Connection) -> None:
        existing = {str(row[1]) for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
        additions = {
            "input_json": "TEXT NOT NULL DEFAULT '{}'",
            "output_json": "TEXT NOT NULL DEFAULT '{}'",
            "error_code": "TEXT NOT NULL DEFAULT ''",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "pause_requested": "INTEGER NOT NULL DEFAULT 0",
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
            "lease_expires_at": "TEXT NOT NULL DEFAULT ''",
            "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
            "resume_after": "TEXT NOT NULL DEFAULT ''",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in existing:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def add_root(self, path: str, recursive: bool = True) -> int:
        normalized = str(Path(path).resolve())
        with self.connection() as db:
            db.execute(
                "INSERT INTO library_roots(path,recursive,created_at) VALUES (?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET recursive=excluded.recursive",
                (normalized, int(recursive), utc_now()),
            )
            return int(
                db.execute("SELECT id FROM library_roots WHERE path=?", (normalized,)).fetchone()[0]
            )

    def roots(self) -> list[tuple[int, str, bool]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,path,recursive FROM library_roots ORDER BY path"
            ).fetchall()
        return [(int(row[0]), str(row[1]), bool(row[2])) for row in rows]

    def upsert_document(
        self,
        *,
        root_id: int,
        path: str,
        document_type: str,
        size_bytes: int,
        modified_ns: int,
        quick_fingerprint: str,
    ) -> tuple[int, str]:
        normalized = str(Path(path).resolve())
        now = utc_now()
        with self.connection() as db:
            existing = db.execute(
                "SELECT id,quick_fingerprint,missing FROM documents WHERE path=?", (normalized,)
            ).fetchone()
            if existing is None:
                cursor = db.execute(
                    "INSERT INTO documents(path,root_id,name,document_type,size_bytes,modified_ns,"
                    "quick_fingerprint,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        normalized,
                        root_id,
                        Path(normalized).name,
                        document_type,
                        size_bytes,
                        modified_ns,
                        quick_fingerprint,
                        now,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("数据库没有返回新增模型记录 ID。")
                document_id, event = int(cursor.lastrowid), "added"
            else:
                document_id = int(existing[0])
                if str(existing[1]) != quick_fingerprint:
                    event = "changed"
                    parameter_status = "stale"
                elif bool(existing[2]):
                    event = "restored"
                    parameter_status = "not_read"
                else:
                    event = "unchanged"
                    parameter_status = None
                sql = (
                    "UPDATE documents SET root_id=?,name=?,document_type=?,size_bytes=?,modified_ns=?,"
                    "quick_fingerprint=?,missing=0,updated_at=?"
                )
                values: list[object] = [
                    root_id,
                    Path(normalized).name,
                    document_type,
                    size_bytes,
                    modified_ns,
                    quick_fingerprint,
                    now,
                ]
                if parameter_status:
                    sql += ",parameter_status=?"
                    values.append(parameter_status)
                sql += " WHERE id=?"
                values.append(document_id)
                db.execute(sql, values)
            if event != "unchanged":
                db.execute(
                    "INSERT INTO document_events(document_id,event,created_at) VALUES (?,?,?)",
                    (document_id, event, now),
                )
        return document_id, event

    def mark_missing(self, root_id: int, seen_paths: set[str]) -> int:
        folded = {str(Path(path).resolve()).casefold() for path in seen_paths}
        changed = 0
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,path FROM documents WHERE root_id=? AND missing=0", (root_id,)
            ).fetchall()
            for row in rows:
                if str(row[1]).casefold() not in folded:
                    db.execute(
                        "UPDATE documents SET missing=1,updated_at=? WHERE id=?",
                        (utc_now(), row[0]),
                    )
                    db.execute(
                        "INSERT INTO document_events(document_id,event,created_at) VALUES (?,?,?)",
                        (row[0], "missing", utc_now()),
                    )
                    changed += 1
            db.execute("UPDATE library_roots SET last_scan_at=? WHERE id=?", (utc_now(), root_id))
        return changed

    def documents(self, query: str = "", limit: int = 200, offset: int = 0) -> list[DocumentRecord]:
        sql = "SELECT * FROM documents"
        params: list[object] = []
        if query.strip():
            sql += " WHERE name LIKE ? OR path LIKE ?"
            value = f"%{query.strip()}%"
            params.extend((value, value))
        sql += " ORDER BY missing,name COLLATE NOCASE LIMIT ? OFFSET ?"
        params.extend((max(1, min(limit, 1000)), max(0, offset)))
        with self.connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [DocumentRecord(**dict(row)) for row in rows]

    def document_count(self, query: str = "") -> int:
        sql = "SELECT COUNT(*) FROM documents"
        params: list[object] = []
        if query.strip():
            sql += " WHERE name LIKE ? OR path LIKE ?"
            value = f"%{query.strip()}%"
            params.extend((value, value))
        with self.connection() as db:
            return int(db.execute(sql, params).fetchone()[0])

    def readable_documents(self) -> list[DocumentRecord]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM documents WHERE missing=0 ORDER BY name COLLATE NOCASE,path"
            ).fetchall()
        return [DocumentRecord(**dict(row)) for row in rows]

    def analyzable_documents(self) -> list[DocumentRecord]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM documents WHERE missing=0 AND parameter_status IN "
                "('ready','metadata_ready') ORDER BY name COLLATE NOCASE,path"
            ).fetchall()
        return [DocumentRecord(**dict(row)) for row in rows]

    def document(self, document_id: int) -> DocumentRecord | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (int(document_id),)).fetchone()
        return DocumentRecord(**dict(row)) if row else None

    def document_by_path(self, path: str) -> DocumentRecord | None:
        if not path:
            return None
        normalized = str(Path(path).resolve(strict=False))
        with self.connection() as db:
            row = db.execute("SELECT * FROM documents WHERE path=?", (normalized,)).fetchone()
        return DocumentRecord(**dict(row)) if row else None

    def set_manifest(
        self,
        document_id: int,
        manifest: dict,
        manifest_sha256: str,
        file_sha256: str,
        *,
        parameter_status: str = "ready",
    ) -> bool:
        if parameter_status not in {"ready", "metadata_ready"}:
            raise ValueError("参数状态必须是 ready 或 metadata_ready。")
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connection() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO manifest_snapshots(document_id,manifest_sha256,file_sha256,"
                "manifest_json,created_at) VALUES (?,?,?,?,?)",
                (document_id, manifest_sha256, file_sha256, encoded, utc_now()),
            )
            db.execute(
                "UPDATE documents SET file_sha256=?,manifest_sha256=?,parameter_status=?,"
                "updated_at=? WHERE id=?",
                (file_sha256, manifest_sha256, parameter_status, utc_now(), document_id),
            )
            return cursor.rowcount > 0

    def update_file_metadata(
        self,
        document_id: int,
        *,
        size_bytes: int,
        modified_ns: int,
        quick_fingerprint: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE documents SET size_bytes=?,modified_ns=?,quick_fingerprint=?,"
                "missing=0,updated_at=? WHERE id=?",
                (size_bytes, modified_ns, quick_fingerprint, utc_now(), document_id),
            )

    def mark_read_failed(self, document_id: int, message: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE documents SET parameter_status='failed',updated_at=? WHERE id=?",
                (utc_now(), document_id),
            )
            db.execute(
                "INSERT INTO document_events(document_id,event,detail,created_at) VALUES (?,?,?,?)",
                (document_id, "read_failed", message[:2000], utc_now()),
            )

    def manifest(self, document_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT manifest_json FROM manifest_snapshots WHERE document_id=? "
                "ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set_tags(self, document_id: int, names: list[str]) -> None:
        normalized = sorted({name.strip() for name in names if name.strip()}, key=str.casefold)
        with self.connection() as db:
            db.execute("DELETE FROM document_tags WHERE document_id=?", (document_id,))
            for name in normalized:
                db.execute(
                    "INSERT OR IGNORE INTO tags(name,created_at) VALUES (?,?)", (name, utc_now())
                )
                tag_id = int(db.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0])
                db.execute(
                    "INSERT INTO document_tags(document_id,tag_id) VALUES (?,?)",
                    (document_id, tag_id),
                )

    def tags(self, document_id: int) -> list[str]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT t.name FROM tags t JOIN document_tags dt ON dt.tag_id=t.id "
                "WHERE dt.document_id=? ORDER BY t.name COLLATE NOCASE",
                (document_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def save_selection_reference(
        self,
        *,
        document_path: str,
        document_title: str,
        alias: str,
        persistent_ref: str,
        selection_type: int,
        object_name: str = "",
    ) -> SelectionReference:
        normalized_path = str(Path(document_path).resolve())
        normalized_alias = str(alias).strip()
        if not normalized_path or not normalized_alias or not persistent_ref:
            raise ValueError("文档路径、引用名称和持久引用不能为空。")
        now = utc_now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO selection_references(document_path,document_title,alias,"
                "persistent_ref,selection_type,object_name,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(document_path,alias) DO UPDATE SET "
                "document_title=excluded.document_title,persistent_ref=excluded.persistent_ref,"
                "selection_type=excluded.selection_type,object_name=excluded.object_name,"
                "updated_at=excluded.updated_at",
                (
                    normalized_path,
                    str(document_title),
                    normalized_alias,
                    persistent_ref,
                    int(selection_type),
                    str(object_name),
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM selection_references WHERE document_path=? AND alias=?",
                (normalized_path, normalized_alias),
            ).fetchone()
        if row is None:
            raise RuntimeError("命名目标引用保存后无法读回。")
        return SelectionReference(**dict(row))

    def selection_references(self, document_path: str = "") -> list[SelectionReference]:
        sql = "SELECT * FROM selection_references"
        params: tuple[object, ...] = ()
        if document_path:
            sql += " WHERE document_path=?"
            params = (str(Path(document_path).resolve()),)
        sql += " ORDER BY document_title COLLATE NOCASE,alias COLLATE NOCASE"
        with self.connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [SelectionReference(**dict(row)) for row in rows]

    def selection_reference(self, reference_id: int) -> SelectionReference | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM selection_references WHERE id=?", (int(reference_id),)
            ).fetchone()
        return SelectionReference(**dict(row)) if row else None

    def delete_selection_reference(self, reference_id: int) -> bool:
        with self.connection() as db:
            cursor = db.execute("DELETE FROM selection_references WHERE id=?", (int(reference_id),))
            return cursor.rowcount > 0
