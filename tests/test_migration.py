import sqlite3

from sw_assistant.infrastructure import HistoryStore
from sw_assistant.library import LibraryRepository
from sw_assistant.migration import LegacyMigrator


def test_legacy_library_import_is_read_only_and_preserves_manifest(tmp_path):
    archive = tmp_path / "old"
    archive.mkdir()
    source = archive / "model_library.db"
    with sqlite3.connect(source) as db:
        db.executescript(
            """
            CREATE TABLE library_scans(id INTEGER PRIMARY KEY,root_path TEXT,recursive INTEGER);
            CREATE TABLE library_files(id INTEGER PRIMARY KEY,path TEXT,document_type TEXT,
              size_bytes INTEGER,modified_ns INTEGER,quick_fingerprint TEXT,is_missing INTEGER);
            CREATE TABLE parameter_snapshots(id INTEGER PRIMARY KEY,file_id INTEGER,version INTEGER,
              manifest_hash TEXT,manifest_json TEXT);
            CREATE TABLE parameter_index_state(file_id INTEGER,file_fingerprint TEXT);
            CREATE TABLE tags(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE file_tags(file_id INTEGER,tag_id INTEGER);
            INSERT INTO library_scans VALUES(1,'C:\\models',1);
            INSERT INTO library_files VALUES(1,'C:\\models\\A.SLDPRT','part',10,1,'quick',0);
            INSERT INTO parameter_snapshots VALUES(1,1,1,'manifest-hash','{"a":1}');
            INSERT INTO parameter_index_state VALUES(1,'file-hash');
            INSERT INTO tags VALUES(1,'pump');
            INSERT INTO file_tags VALUES(1,1);
            """
        )
    destination = LibraryRepository(tmp_path / "new.db")
    report = LegacyMigrator(archive).import_library(destination)
    assert report.imported_documents == 1
    assert report.imported_manifests == 1
    record = destination.documents()[0]
    assert destination.manifest(record.id) == {"a": 1}
    assert destination.tags(record.id) == ["pump"]
    assert source.is_file()


def test_legacy_execution_history_is_mapped_without_mutating_source(tmp_path):
    archive = tmp_path / "old"
    archive.mkdir()
    source = archive / "experience.db"
    with sqlite3.connect(source) as db:
        db.executescript(
            """
            CREATE TABLE execution_runs(
              id INTEGER PRIMARY KEY,request_text TEXT,design_plan TEXT,result TEXT,
              started_at TEXT,finished_at TEXT);
            CREATE TABLE command_events(
              id INTEGER PRIMARY KEY,run_id INTEGER,status TEXT,error_message TEXT);
            INSERT INTO execution_runs VALUES(
              7,'draw part','legacy design','success','2026-01-01','2026-01-02');
            INSERT INTO command_events VALUES(1,7,'failed','old transient failure');
            """
        )
    history = HistoryStore(tmp_path / "history.db")
    imported = LegacyMigrator(archive).import_history(history)
    assert imported == 1
    row = history.recent()[0]
    assert row["id"] == "legacy-7"
    assert row["state"] == "completed"
    assert row["error"] == "old transient failure"
    assert source.is_file()
