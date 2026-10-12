from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sw_assistant.solidworks.checkpoint_store import CheckpointStore
from test_gateway_checkpoints import Document, Gateway, begin, observation


class DurableDocument(Document):
    def save_as(self, path, version, options, *_):
        result = super().save_as(path, version, options)
        Path(path).write_text(json.dumps({"volume": self.volume}), encoding="utf-8")
        return result


def durable_gateway(root, source=None):
    gateway = Gateway(source or DurableDocument())

    def open_native(path, kind, options, config, *_):
        assert kind == 1 and options == 1 and config == "Default"
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        document = DurableDocument(Path(path).stem, str(path), volume=data["volume"])
        gateway._sw.ActiveDoc = document
        gateway._sw.documents.append(document)
        if gateway._sw.on_open:
            gateway._sw.on_open(document)
        return document, *gateway._sw.open_codes

    gateway._sw.OpenDoc6 = open_native
    gateway.configure_candidate_workspace(root)
    return gateway


@pytest.fixture
def durable(tmp_path):
    gateway = durable_gateway(tmp_path / "workspace")
    gateway.register_candidate_document(gateway._sw.ActiveDoc)
    return gateway


def commit(gateway, *, volume=2e-6, task="task-1"):
    trial = begin(gateway, task)
    assert trial["ok"], trial
    candidate = gateway._sw.ActiveDoc
    candidate.volume = volume
    candidate.stamp += 1
    result = gateway.finish_candidate(trial["token"], True, observation(gateway))
    assert result["ok"], result
    return result


def test_commit_saves_final_memory_not_initial_candidate_file(durable):
    result = commit(durable)
    initial = json.loads(Path(result["artifact_path"]).read_text())
    checkpoint = json.loads(Path(result["checkpoint_path"]).read_text())
    assert initial["volume"] == 1e-6
    assert checkpoint["volume"] == 2e-6
    manifest = CheckpointStore(durable._candidate_root).read("task-1", result["checkpoint_id"])
    assert manifest["observation"]["snapshot"]["volume_m3"] == 2e-6
    assert result["persisted_current_state"] is True
    assert durable._sw.ActiveDoc.saved_options[0][1:] == (0, 11)


def test_parent_chain_and_task_scoped_list(durable):
    first = commit(durable)
    second = commit(durable, volume=3e-6)
    assert second["checkpoint_parent_id"] == first["checkpoint_id"]
    listed = durable.checkpoint_request("list", "task-1")
    assert listed["ok"] and listed["issues"] == []
    assert [item["checkpoint_id"] for item in listed["checkpoints"]] == [first["checkpoint_id"], second["checkpoint_id"]]
    assert all("observation" not in item and "geometry" not in item for item in listed["checkpoints"])
    assert durable.checkpoint_request("list", "other")["checkpoints"] == []


def test_explicit_restore_branches_without_mutating_checkpoint_or_current_model(durable):
    first = commit(durable)
    second = commit(durable, volume=3e-6)
    previous = durable._sw.ActiveDoc
    native = Path(first["checkpoint_path"]).read_bytes()
    restored = durable.checkpoint_request("restore", "task-1", first["checkpoint_id"])
    assert restored["ok"] and restored["status"] == "restored", restored
    assert restored["observation"]["snapshot"]["volume_m3"] == 2e-6
    assert previous.volume == 3e-6 and previous in durable._sw.documents
    assert Path(first["checkpoint_path"]).read_bytes() == native
    assert restored["artifact_path"] not in {first["checkpoint_path"], second["checkpoint_path"]}
    third = commit(durable, volume=4e-6)
    assert third["checkpoint_parent_id"] == first["checkpoint_id"]


def test_new_worker_can_restore_only_when_no_active_document(durable):
    saved = commit(durable)
    old = durable._sw.ActiveDoc
    new = durable_gateway(durable._candidate_root, old)
    denied = new.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not denied["ok"] and new._sw.ActiveDoc is old
    new._sw.ActiveDoc = None
    restored = new.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert restored["ok"], restored
    assert old.volume == 2e-6 and old in new._sw.documents
    assert new._candidate_owned[new._sw.ActiveDoc.identity].checkpoint_id == saved["checkpoint_id"]


@pytest.mark.parametrize("failure", ["save_error", "save_warning", "stamp", "geometry", "selection", "source", "active", "publish"])
def test_failed_persistence_does_not_commit_or_adopt(durable, failure, monkeypatch):
    trial = begin(durable)
    candidate = durable._sw.ActiveDoc
    original = durable._candidate_active.source
    if failure in {"save_error", "save_warning"}:
        candidate.save_result = (failure != "save_error", 0, int(failure == "save_warning"))
    elif failure == "stamp":
        candidate.on_save = lambda: setattr(candidate, "stamp", candidate.stamp + 1)
    elif failure == "geometry":
        candidate.on_save = lambda: setattr(candidate, "volume", candidate.volume * 2)
    elif failure == "selection":
        candidate.on_save = lambda: setattr(candidate, "selection", 1)
    elif failure == "source":
        candidate.on_save = lambda: setattr(original, "stamp", original.stamp + 1)
    elif failure == "active":
        candidate.on_save = lambda: setattr(durable._sw, "ActiveDoc", Document("foreign"))
    else:
        def denied(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(CheckpointStore, "publish", denied)
    result = durable.finish_candidate(trial["token"], True, observation(durable))
    assert not result["ok"], result
    assert durable._candidate_active is not None
    assert candidate.identity not in durable._candidate_owned
    assert durable.checkpoint_request("list", "task-1")["checkpoints"] == []
    assert durable._sw.activations == []


@pytest.mark.parametrize("change", ["stamp", "config", "volume", "selection", "active_sketch", "task", "active_trial"])
def test_restore_refuses_changed_or_pending_owned_document(durable, change):
    saved = commit(durable)
    candidate = durable._sw.ActiveDoc
    task = "task-1"
    if change == "stamp":
        candidate.stamp += 1
    elif change == "config":
        candidate.config.Name = "Other"
    elif change == "volume":
        candidate.volume *= 2
    elif change == "selection":
        candidate.selection = 1
    elif change == "active_sketch":
        candidate.active_sketch = "Sketch1"
    elif change == "task":
        task = "other"
    else:
        assert begin(durable)["ok"]
    count = len(durable._sw.documents)
    result = durable.checkpoint_request("restore", task, saved["checkpoint_id"])
    assert not result["ok"]
    assert len(durable._sw.documents) == count
    assert durable._sw.activations == []


@pytest.mark.parametrize("change", ["native", "size", "task", "id", "schema", "path", "nan", "duplicate", "observation", "geometry"])
def test_corrupt_checkpoint_never_gains_ownership(durable, change):
    saved = commit(durable)
    path = Path(saved["checkpoint_path"])
    manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if change == "native":
        path.write_bytes(b"corrupted native bytes")
    elif change == "duplicate":
        text = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(text[:-1] + ',"schema":1}', encoding="utf-8")
    else:
        if change == "size":
            manifest["native_size"] = True
        elif change == "task":
            manifest["task_id"] = "other"
        elif change == "id":
            manifest["checkpoint_id"] = "../escape"
        elif change == "schema":
            manifest["schema"] = True
        elif change == "path":
            manifest["native_path"] = "C:/user-part.sldprt"
        elif change == "nan":
            manifest["geometry"]["injected"] = float("nan")
        elif change == "observation":
            manifest["observation"]["snapshot"]["volume_m3"] *= 2
        else:
            manifest["geometry"]["extents"]["extents_mm"]["x"] *= 2
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    old_owned = set(durable._candidate_owned)
    result = durable.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not result["ok"], result
    assert set(durable._candidate_owned) == old_owned
    assert durable._sw.activations == []


def test_restore_open_warning_keeps_copy_without_adoption_or_close(durable):
    saved = commit(durable)
    old_owned = set(durable._candidate_owned)
    count = len(durable._sw.documents)
    durable._sw.open_codes = (0, 1)
    result = durable.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not result["ok"] and result["working_copy_preserved"] is True
    assert Path(result["artifact_path"]).is_file()
    assert set(durable._candidate_owned) == old_owned
    assert len(durable._sw.documents) == count + 1


def test_restore_detects_external_switch_during_open(durable):
    saved = commit(durable)
    external = Document("external")
    durable._sw.on_open = lambda _: setattr(durable._sw, "ActiveDoc", external)
    result = durable.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not result["ok"] and durable._sw.ActiveDoc is external
    assert not durable._sw.activations


@pytest.mark.parametrize("operation,task,key", [
    ("delete", "task-1", None), ("list", "task-1", "0" * 32),
    ("restore", "task-1", "../escape"), ("restore", "task-1", None),
    ("restore", "", "0" * 32), ("restore", "x\0y", "0" * 32),
])
def test_private_request_contract_rejects_invalid_fields(durable, operation, task, key):
    assert not durable.checkpoint_request(operation, task, key)["ok"]
    assert len(durable._sw.documents) == 1


def test_unpublished_native_artifacts_are_not_listed(durable):
    store = CheckpointStore(durable._candidate_root)
    _, path = store.reserve("task-1")
    path.write_bytes(b"save interrupted")
    (path.parent / "manifest-interrupted.tmp").write_text("{}")
    assert store.list("task-1")["checkpoints"] == []


def test_hash_directory_prevents_task_path_injection(durable):
    result = commit(durable, task="../../outside")
    assert Path(result["checkpoint_path"]).is_relative_to(durable._candidate_root)
    assert "outside" not in result["checkpoint_path"]
    assert durable.checkpoint_request("list", "../../outside")["checkpoints"]


@pytest.mark.parametrize("kind", ["native", "manifest"])
def test_hardlinked_files_are_not_trusted(durable, tmp_path, kind):
    saved = commit(durable)
    path = Path(saved["checkpoint_path"])
    target = path if kind == "native" else path.parent / "manifest.json"
    os.link(target, tmp_path / "external-link")
    result = durable.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not result["ok"] and "linked" in result["reason"]
    listed = durable.checkpoint_request("list", "task-1")
    assert listed["checkpoints"] == [] and len(listed["issues"]) == 1


def test_publish_disk_delay_cannot_adopt_changed_candidate(durable, monkeypatch):
    trial = begin(durable)
    candidate = durable._sw.ActiveDoc
    publish = CheckpointStore.publish

    def changed(store, *args, **kwargs):
        result = publish(store, *args, **kwargs)
        candidate.stamp += 1
        return result

    monkeypatch.setattr(CheckpointStore, "publish", changed)
    result = durable.finish_candidate(trial["token"], True, observation(durable))
    assert not result["ok"]
    assert candidate.identity not in durable._candidate_owned
    # The already-published file remains a verified earlier checkpoint, not
    # evidence that the externally changed active document was committed.
    assert len(durable.checkpoint_request("list", "task-1")["checkpoints"]) == 1


def test_restore_disk_verification_cannot_adopt_changed_copy(durable, monkeypatch):
    saved = commit(durable)
    read = CheckpointStore.read
    old = durable._sw.ActiveDoc

    def changed(store, *args, **kwargs):
        result = read(store, *args, **kwargs)
        current = durable._sw.ActiveDoc
        if current is not old:
            current.stamp += 1
        return result

    monkeypatch.setattr(CheckpointStore, "read", changed)
    result = durable.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not result["ok"]
    assert durable._sw.ActiveDoc.identity not in durable._candidate_owned


def test_manifest_cannot_redirect_native_filename(durable):
    saved = commit(durable)
    path = Path(saved["checkpoint_path"])
    original = path.read_bytes()
    path.with_name("other.sldprt").write_bytes(original)
    path.write_bytes(b"changed")
    result = durable.checkpoint_request("restore", "task-1", saved["checkpoint_id"])
    assert not result["ok"]
