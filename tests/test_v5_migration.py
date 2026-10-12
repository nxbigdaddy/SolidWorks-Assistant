from __future__ import annotations

import json
import sqlite3

from sw_assistant.infrastructure.paths import AppPaths
from sw_assistant.infrastructure.v5_migration import import_previous_data
from sw_assistant.infrastructure import v5_migration


def test_import_keeps_protected_settings_and_does_not_modify_source(tmp_path):
    previous = tmp_path / "previous"
    previous.mkdir()
    settings = b'{"version":1,"model":{"api_key_protected":"dpapi:opaque"}}'
    (previous / "settings.json").write_bytes(settings)
    with sqlite3.connect(previous / "library.db") as db:
        db.execute("CREATE TABLE records (name TEXT)")
        db.execute("INSERT INTO records VALUES ('本地参数')")
    before = (previous / "library.db").read_bytes()
    destination = tmp_path / "v5"
    report = import_previous_data(previous, destination)
    assert report == {"imported": ["settings.json", "library.db"], "errors": []}
    assert (destination / "settings.json").read_bytes() == settings
    assert (previous / "settings.json").read_bytes() == settings
    assert (previous / "library.db").read_bytes() == before
    with sqlite3.connect(destination / "library.db") as db:
        assert db.execute("SELECT name FROM records").fetchone()[0] == "本地参数"
    (previous / "settings.json").write_text("changed", encoding="utf-8")
    import_previous_data(previous, destination)
    assert (destination / "settings.json").read_bytes() == settings
    assert "opaque" not in json.dumps(report)


def test_import_preserves_existing_v5_files(tmp_path):
    previous, destination = tmp_path / "previous", tmp_path / "v5"
    previous.mkdir()
    destination.mkdir()
    (previous / "settings.json").write_text("old", encoding="utf-8")
    (destination / "settings.json").write_text("new", encoding="utf-8")
    import_previous_data(previous, destination)
    assert (destination / "settings.json").read_text("utf-8") == "new"


def test_default_v5_paths_isolate_data_and_share_readonly_event_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("SW_ASSISTANT_DATA_DIR", raising=False)
    monkeypatch.delenv("SW_ASSISTANT_EVENT_BRIDGE_DIR", raising=False)
    paths = AppPaths.default()
    assert paths.data == tmp_path / "SolidWorksAIAssistantV5"
    assert paths.event_bridge == tmp_path / "SolidWorksAIAssistant" / "event-bridge"
    monkeypatch.setenv("SW_ASSISTANT_DATA_DIR", str(tmp_path / "test-data"))
    paths = AppPaths.default()
    paths.ensure()
    assert not (paths.data / "v4-import.json").exists()


def test_partial_import_retries_failed_file_and_preserves_successful_target(tmp_path, monkeypatch):
    previous, destination = tmp_path / "previous", tmp_path / "v5"
    previous.mkdir()
    (previous / "settings.json").write_text("protected settings", encoding="utf-8")
    with sqlite3.connect(previous / "library.db") as db:
        db.execute("CREATE TABLE samples (id INTEGER)")
    original_copy = v5_migration.shutil.copyfile
    def unavailable(*_args, **_kwargs):
        raise PermissionError("synthetic lock")
    monkeypatch.setattr(v5_migration.shutil, "copyfile", unavailable)
    first = import_previous_data(previous, destination)
    assert first["errors"] == ["settings.json: PermissionError"]
    assert first["imported"] == ["library.db"]
    with sqlite3.connect(destination / "library.db") as db:
        db.execute("INSERT INTO samples VALUES (42)")
    monkeypatch.setattr(v5_migration.shutil, "copyfile", original_copy)
    second = import_previous_data(previous, destination)
    assert second == {"imported": ["settings.json"], "errors": []}
    with sqlite3.connect(destination / "library.db") as db:
        assert db.execute("SELECT id FROM samples").fetchone()[0] == 42
    marker = json.loads((destination / "v4-import.json").read_text("utf-8"))
    assert marker["previous_errors"] == first["errors"]
    assert set(marker["imported"]) == {"settings.json", "library.db"}
    assert marker["errors"] == []


def test_failed_import_never_overwrites_later_created_target(tmp_path, monkeypatch):
    previous, destination = tmp_path / "previous", tmp_path / "v5"
    previous.mkdir()
    (previous / "settings.json").write_text("old", encoding="utf-8")
    original_copy = v5_migration.shutil.copyfile
    def unavailable(*_args, **_kwargs):
        raise PermissionError("synthetic lock")
    monkeypatch.setattr(v5_migration.shutil, "copyfile", unavailable)
    assert import_previous_data(previous, destination)["errors"]
    (destination / "settings.json").write_text("new user's settings", encoding="utf-8")
    monkeypatch.setattr(v5_migration.shutil, "copyfile", original_copy)
    report = import_previous_data(previous, destination)
    assert report["errors"] == ["settings.json: ExistingTargetPreserved"]
    assert (destination / "settings.json").read_text("utf-8") == "new user's settings"


def test_invalid_import_marker_does_not_block_missing_file_recovery(tmp_path):
    previous, destination = tmp_path / "previous", tmp_path / "v5"
    previous.mkdir()
    destination.mkdir()
    (previous / "settings.json").write_text("old", encoding="utf-8")
    (destination / "v4-import.json").write_text("{interrupted", encoding="utf-8")
    report = import_previous_data(previous, destination)
    assert report == {"imported": ["settings.json"], "errors": []}
    assert (destination / "settings.json").read_text("utf-8") == "old"
