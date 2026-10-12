from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sw_assistant.advanced import (
    CapabilityState,
    CapabilityStatus,
    ContextSnapshot,
    FamilyVariant,
    LocalSemanticIndex,
    ModelHealthService,
    ProductFamilyService,
    ProductFamilySpec,
)
from sw_assistant.automation import PersistentBatchRunner, PersistentJobStore
from sw_assistant.library import LibraryRepository


def _document(repository: LibraryRepository, tmp_path, name: str = "轴承座.SLDPRT"):
    root_id = repository.add_root(str(tmp_path))
    path = tmp_path / name
    path.write_bytes(b"solidworks-fixture")
    stat = path.stat()
    document_id, _ = repository.upsert_document(
        root_id=root_id,
        path=str(path),
        document_type="part",
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        quick_fingerprint="quick",
    )
    return repository.document(document_id)


def test_schema_two_migrates_jobs_without_losing_rows(tmp_path):
    path = tmp_path / "library.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_info(version INTEGER NOT NULL);
        INSERT INTO schema_info(version) VALUES (2);
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO jobs(id,kind,state,created_at,updated_at)
        VALUES ('legacy','scan','queued','2026-01-01','2026-01-01');
        """
    )
    connection.commit()
    connection.close()

    repository = LibraryRepository(path)
    with repository.connection() as db:
        assert db.execute("SELECT version FROM schema_info").fetchone()[0] == 3
        row = db.execute(
            "SELECT id,input_json,pause_requested,attempt FROM jobs WHERE id='legacy'"
        ).fetchone()
    assert tuple(row) == ("legacy", "{}", 0, 0)


def test_persistent_job_lifecycle_and_expired_lease_recovery(tmp_path):
    store = PersistentJobStore(LibraryRepository(tmp_path / "library.db"))
    created = store.create("export", [{"idempotency_key": "a", "path": "a"}])
    claimed = store.claim("worker", lease_seconds=5)
    assert claimed and claimed.id == created.id and claimed.attempt == 1
    item = store.next_item(created.id, "worker")
    assert item and item.input["path"] == "a"
    store.finish_item(item.id, "worker", output={"destination": "a.step"})
    finished = store.finish_job(created.id, "worker")
    assert (finished.state, finished.processed, finished.succeeded) == ("succeeded", 1, 1)

    second = store.create("export", [{"path": "b"}])
    assert store.claim("crashed", lease_seconds=5).id == second.id
    with store.repository.connection() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), second.id),
        )
    assert store.recover_expired() == 1
    assert store.get(second.id).state == "queued"
    assert store.claim("replacement", lease_seconds=5).id == second.id


def test_queued_job_can_be_paused_and_resumed_without_becoming_stranded(tmp_path):
    store = PersistentJobStore(LibraryRepository(tmp_path / "library.db"))
    job = store.create("export", [{"path": "a"}])

    assert store.request_pause(job.id) is True
    paused = store.get(job.id)
    assert paused is not None
    assert paused.state == "paused"
    assert paused.pause_requested is False
    assert store.claim("worker", lease_seconds=5) is None

    assert store.resume(job.id) is True
    claimed = store.claim("worker", lease_seconds=5)
    assert claimed is not None and claimed.id == job.id


def test_persistent_runner_continues_when_requested(tmp_path):
    store = PersistentJobStore(LibraryRepository(tmp_path / "library.db"))
    job = store.create(
        "demo",
        [{"value": 1}, {"value": 2}],
        input={"continue_on_error": True},
    )

    def handler(_job, item, _cancel):
        if item.input["value"] == 1:
            raise ValueError("bad item")
        return {"value": item.input["value"] * 2}

    result = PersistentBatchRunner(store, {"demo": handler}, owner="test").run_once()
    assert result and result.id == job.id and result.state == "succeeded"
    assert (result.processed, result.failed, result.succeeded) == (2, 1, 1)


def test_persistent_runner_processes_one_thousand_items_idempotently(tmp_path):
    store = PersistentJobStore(LibraryRepository(tmp_path / "library.db"))
    job = store.create(
        "stress",
        [{"idempotency_key": f"item-{index}", "value": index} for index in range(1000)],
    )
    observed: list[int] = []

    def handler(_job, item, _cancel):
        observed.append(int(item.input["value"]))
        return {"value": item.input["value"]}

    runner = PersistentBatchRunner(store, {"stress": handler}, owner="stress-test")
    result = runner.run_once()

    assert result and result.id == job.id and result.state == "succeeded"
    assert (result.processed, result.succeeded, result.failed) == (1000, 1000, 0)
    assert observed == list(range(1000))
    assert runner.run_once() is None


def test_context_snapshot_rejects_stale_or_busy_write():
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    stale = ContextSnapshot("session", 1, old, document={"title": "part"})
    assert stale.usable_for_write()[0] is False
    busy = ContextSnapshot(
        "session",
        2,
        datetime.now(timezone.utc).isoformat(),
        command_in_progress=True,
        document={"title": "part"},
        capabilities=(CapabilityStatus("write", CapabilityState.AVAILABLE, "interop present"),),
    )
    assert "正在执行" in busy.usable_for_write()[1]


def test_health_report_combines_feature_reference_and_policy_issues():
    report = ModelHealthService().diagnose(
        {
            "path": "bad.SLDPRT",
            "document_type": "part",
            "features": [{"name": "Boss", "type": "Extrude", "error_code": 1}],
            "references": [{"path": "missing.SLDPRT", "resolved": False}],
            "material": "",
            "geometry": {"body_count": 0},
        }
    )
    assert report.status == "critical"
    assert report.score < 50
    assert {issue.rule_id for issue in report.issues} >= {
        "feature.rebuild_error",
        "reference.missing",
        "part.material_missing",
        "part.no_body",
    }


def test_local_semantic_index_is_persistent_and_explainable(tmp_path):
    repository = LibraryRepository(tmp_path / "library.db")
    record = _document(repository, tmp_path)
    assert record is not None
    manifest = {
        "path": record.path,
        "document_type": "part",
        "material": "不锈钢",
        "features": [{"name": "安装孔", "type": "HoleWizard"}],
        "dimensions": [{"name": "孔径", "display_value_mm": 8}],
        "geometry": {"body_count": 1},
    }
    repository.set_manifest(record.id, manifest, "manifest", "file")
    index = LocalSemanticIndex(repository)
    assert index.rebuild()["indexed"] == 1
    hits = index.search("不锈钢 安装孔")
    assert hits and hits[0].document_id == record.id
    assert any("材料匹配" in reason or "特征匹配" in reason for reason in hits[0].reasons)
    assert LocalSemanticIndex(repository).search("不锈钢")[0].path == record.path


def test_product_family_validates_and_verifies_adapter_result(tmp_path):
    source = tmp_path / "source.SLDPRT"
    source.write_bytes(b"part")
    destination = tmp_path / "family.SLDPRT"
    spec = ProductFamilySpec(
        str(source),
        str(destination),
        (
            FamilyVariant("M8", {"D1@Sketch1": 8.0}, {"规格": "M8"}),
            FamilyVariant("M10", {"D1@Sketch1": 10.0}, {"规格": "M10"}),
        ),
    )

    class Adapter:
        def generate_product_family(self, value, *, cancel: threading.Event, progress=None):
            assert value is spec and not cancel.is_set()
            if progress:
                progress(2, 2, "done")
            return {"configurations": ["M8", "M10"]}

    assert ProductFamilyService(Adapter()).generate(spec)["configurations"] == ["M8", "M10"]
    invalid = ProductFamilySpec(
        str(source),
        str(destination),
        (
            FamilyVariant("same", {"D1": 1}, {}),
            FamilyVariant("SAME", {"D1": 2}, {}),
        ),
    )
    with pytest.raises(ValueError, match="重复"):
        invalid.validate()
