import json
import os
import sqlite3
import threading

from sw_assistant.domain import ModelSnapshot
from sw_assistant.library import LibraryRepository, LibraryService

from fakes import FakeWorker, result


def test_fast_scan_indexes_supported_files_without_worker(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    (root / "A.SLDPRT").write_bytes(b"part")
    (root / "B.SLDASM").write_bytes(b"assembly")
    (root / "ignore.txt").write_text("x")
    worker = FakeWorker()
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, worker)
    outcome = service.scan(root)
    assert outcome["succeeded"] == 2
    assert len(repository.documents()) == 2
    assert worker.calls == []


def test_cancelled_scan_does_not_mark_unseen_files_missing(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    (root / "A.SLDPRT").write_bytes(b"a")
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, FakeWorker())
    service.scan(root)
    cancel = threading.Event()
    cancel.set()
    outcome = service.scan(root, cancel=cancel)
    assert outcome["cancelled"] is True
    assert repository.documents()[0].missing is not True


def test_deep_read_saves_manifest_and_hash(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    path = root / "A.SLDPRT"
    path.write_bytes(b"part")
    repository = LibraryRepository(tmp_path / "library.db")
    manifest = {"schema_version": 1, "dimensions": [{"name": "D1", "value": 1}]}
    worker = FakeWorker(
        [
            result(
                before=ModelSnapshot(True),
                after=ModelSnapshot(True),
                data={"manifest": manifest},
            )
        ]
    )
    service = LibraryService(repository, worker)
    service.scan(root)
    record = repository.documents()[0]
    outcome = service.deep_read([record])
    updated = repository.documents()[0]
    assert outcome["succeeded"] == 1
    assert updated.parameter_status == "ready"
    assert len(updated.manifest_sha256) == 64
    assert repository.manifest(updated.id) == json.loads(json.dumps(manifest))

    second_worker = FakeWorker()
    second = LibraryService(repository, second_worker).deep_read([updated])
    assert second["skipped"] == 1
    assert second_worker.calls == []


def test_manifest_snapshots_are_deduplicated(tmp_path):
    repository = LibraryRepository(tmp_path / "library.db")
    root_id = repository.add_root(str(tmp_path))
    document_id, _ = repository.upsert_document(
        root_id=root_id,
        path=str(tmp_path / "A.SLDPRT"),
        document_type="part",
        size_bytes=1,
        modified_ns=1,
        quick_fingerprint="quick",
    )
    assert repository.set_manifest(document_id, {"a": 1}, "hash", "file") is True
    assert repository.set_manifest(document_id, {"a": 1}, "hash", "file") is False


def test_parameter_hash_ignores_capture_location_and_time():
    first = {
        "path": r"C:\one\A.SLDPRT",
        "title": "A.SLDPRT",
        "captured_at": "2026-01-01",
        "dimensions": [{"name": "D1", "system_value": 0.01}],
    }
    second = {
        **first,
        "path": r"D:\other\copy.SLDPRT",
        "title": "copy.SLDPRT",
        "captured_at": "2026-02-02",
    }
    assert LibraryService.manifest_sha256(first) == LibraryService.manifest_sha256(second)


def test_deep_read_source_is_not_limited_by_ui_page_size(tmp_path):
    repository = LibraryRepository(tmp_path / "library.db")
    root_id = repository.add_root(str(tmp_path))
    for index in range(205):
        repository.upsert_document(
            root_id=root_id,
            path=str(tmp_path / f"Part-{index:03}.SLDPRT"),
            document_type="part",
            size_bytes=1,
            modified_ns=index,
            quick_fingerprint=str(index),
        )
    first_page = repository.documents(limit=200)
    assert len(first_page) == 200
    assert len(repository.readable_documents()) == 205


def test_incomplete_directory_walk_does_not_mark_existing_records_missing(
    tmp_path, monkeypatch
):
    root = tmp_path / "models"
    root.mkdir()
    source = root / "A.SLDPRT"
    source.write_bytes(b"part")
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, FakeWorker())
    service.scan(root)

    def denied_walk(_root, **kwargs):
        kwargs["onerror"](PermissionError("denied"))
        yield str(root), [], []

    monkeypatch.setattr("sw_assistant.library.service.os.walk", denied_walk)
    outcome = service.scan(root)
    assert outcome["incomplete"] is True
    assert outcome["failed"] == 1
    assert repository.documents()[0].missing is not True


def test_nonrecursive_discovery_error_is_reported_without_marking_missing(
    tmp_path, monkeypatch
):
    root = tmp_path / "models"
    root.mkdir()
    source = root / "A.SLDPRT"
    source.write_bytes(b"part")
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, FakeWorker())
    service.scan(root, recursive=False)

    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr("sw_assistant.library.service.Path.iterdir", denied)
    outcome = service.scan(root, recursive=False)
    assert outcome["incomplete"] is True
    assert outcome["failed"] == 1
    assert repository.documents()[0].missing is not True


def test_deep_read_uses_full_file_hash_before_skipping(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    path = root / "A.SLDPRT"
    path.write_bytes(b"a" * 65536 + b"b" * 65536 + b"c" * 65536)
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(
        repository,
        FakeWorker([result(data={"manifest": {"dimensions": [{"name": "D1"}]}})]),
    )
    service.scan(root)
    service.deep_read(repository.documents())
    record = repository.documents()[0]
    original_stat = path.stat()

    with path.open("r+b") as stream:
        stream.seek(65536 + 100)
        stream.write(b"changed")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    worker = FakeWorker(
        [result(data={"manifest": {"dimensions": [{"name": "D1", "value": 2}]}})]
    )
    outcome = LibraryService(repository, worker).deep_read([record])
    assert outcome["succeeded"] == 1
    assert outcome["skipped"] == 0
    assert len(worker.calls) == 1


def test_cancelled_hash_does_not_mark_document_read_failed(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    (root / "A.SLDPRT").write_bytes(b"part")
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, FakeWorker())
    service.scan(root)
    record = repository.documents()[0]
    cancel = threading.Event()

    def interrupted(_path, _cancel):
        cancel.set()
        raise InterruptedError("stop")

    monkeypatch.setattr(service, "file_sha256", interrupted)
    outcome = service.deep_read([record], cancel=cancel)
    assert outcome["cancelled"] is True
    assert outcome["failed"] == 0
    assert repository.documents()[0].parameter_status == "not_read"


def test_deep_read_rejects_file_changed_while_solidworks_reads_it(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    path = root / "A.SLDPRT"
    path.write_bytes(b"before")
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, FakeWorker())
    service.scan(root)
    record = repository.documents()[0]

    class MutatingWorker:
        def call(self, _call, _timeout_seconds=120):
            path.write_bytes(b"after")
            return result(data={"manifest": {"dimensions": []}})

    outcome = LibraryService(repository, MutatingWorker()).deep_read([record])
    assert outcome["failed"] == 1
    assert repository.documents()[0].parameter_status == "failed"


def test_content_equal_touch_refreshes_metadata_without_solidworks(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    path = root / "A.SLDPRT"
    path.write_bytes(b"part")
    repository = LibraryRepository(tmp_path / "library.db")
    initial_worker = FakeWorker([result(data={"manifest": {"dimensions": []}})])
    service = LibraryService(repository, initial_worker)
    service.scan(root)
    service.deep_read(repository.documents())
    record = repository.documents()[0]
    os.utime(
        path,
        ns=(path.stat().st_atime_ns, record.modified_ns + 1_000_000_000),
    )

    worker = FakeWorker()
    outcome = LibraryService(repository, worker).deep_read([record])
    refreshed = repository.documents()[0]
    assert outcome["skipped"] == 1
    assert worker.calls == []
    assert refreshed.modified_ns != record.modified_ns
    assert refreshed.file_sha256 == record.file_sha256


def test_schema_one_database_upgrades_and_stores_named_selection_reference(tmp_path):
    database = tmp_path / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE schema_info(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info(version) VALUES (1)")
    repository = LibraryRepository(database)
    source = tmp_path / "Part1.SLDPRT"
    source.write_bytes(b"part")
    saved = repository.save_selection_reference(
        document_path=str(source),
        document_title="Part1.SLDPRT",
        alias="安装面",
        persistent_ref="AQID",
        selection_type=2,
        object_name="Face1",
    )
    assert saved.alias == "安装面"
    assert repository.selection_reference(saved.id) == saved
    assert repository.selection_references(str(source)) == [saved]

    updated = repository.save_selection_reference(
        document_path=str(source),
        document_title="Part1.SLDPRT",
        alias="安装面",
        persistent_ref="BAUG",
        selection_type=2,
        object_name="Face2",
    )
    assert updated.id == saved.id
    assert updated.persistent_ref == "BAUG"
    assert repository.delete_selection_reference(saved.id) is True
    assert repository.selection_reference(saved.id) is None


def test_document_manager_metadata_does_not_skip_later_full_com_read(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    path = root / "A.SLDPRT"
    path.write_bytes(b"part")
    repository = LibraryRepository(tmp_path / "library.db")
    service = LibraryService(repository, FakeWorker())
    service.scan(root)

    class Reader:
        configured = True

        @staticmethod
        def inspect(_path):
            return {
                "schema_version": 2,
                "reader": "document_manager",
                "completeness": "metadata",
                "custom_properties": {"PartNumber": "P-1"},
            }

    metadata = service.metadata_read(repository.documents(), Reader())
    record = repository.documents()[0]
    assert metadata["succeeded"] == 1
    assert record.parameter_status == "metadata_ready"

    worker = FakeWorker(
        [result(data={"manifest": {"schema_version": 2, "features": []}})]
    )
    full = LibraryService(repository, worker).deep_read([record])
    assert full["succeeded"] == 1
    assert len(worker.calls) == 1
    assert repository.documents()[0].parameter_status == "ready"
