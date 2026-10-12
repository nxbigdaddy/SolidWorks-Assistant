from __future__ import annotations

import json
import time

from sw_assistant.solidworks.event_bridge import (
    AddinEvent,
    EventBridgeMonitor,
    capability_snapshot_matches,
    read_capability_snapshot,
)
from sw_assistant.solidworks import SolidWorksContextStore


def _event(event_id: str, name: str = "document.opened") -> dict:
    return {
        "schema_version": 1,
        "sequence": 1,
        "session_id": "session",
        "event_id": event_id,
        "timestamp_utc": "2026-07-28T12:00:00.0000000Z",
        "process_id": 123,
        "source": "solidworks-addin",
        "name": name,
        "payload": {"title": "测试零件"},
    }


def test_addin_event_validates_protocol():
    event = AddinEvent.from_mapping(_event("one"))
    assert event.name == "document.opened"
    assert event.payload["title"] == "测试零件"


def test_monitor_reads_complete_lines_and_deduplicates(tmp_path):
    path = tmp_path / "events.jsonl"
    received = []
    monitor = EventBridgeMonitor(path, received.append, start_at_end=False)
    first = json.dumps(_event("one"), ensure_ascii=False).encode("utf-8")
    second = json.dumps(_event("two"), ensure_ascii=False).encode("utf-8")
    path.write_bytes(first + b"\n" + second)

    assert monitor.poll_once() == 1
    assert [event.event_id for event in received] == ["one"]
    with path.open("ab") as stream:
        stream.write(b"\n" + first + b"\n")
    assert monitor.poll_once() == 1
    assert [event.event_id for event in received] == ["one", "two"]


def test_monitor_handles_rotation_and_background_delivery(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("", "utf-8")
    received = []
    monitor = EventBridgeMonitor(path, received.append, poll_seconds=0.02)
    monitor.start()
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_event("live"), ensure_ascii=False) + "\n")
        deadline = time.monotonic() + 1
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [event.event_id for event in received] == ["live"]
    finally:
        monitor.stop()
    assert monitor.running is False


def test_capability_snapshot_reader(tmp_path):
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "solidworks_revision": "33.5.0.53",
                "capabilities": [{"id": "events.active_document", "available": True}],
            }
        ),
        "utf-8",
    )
    snapshot = read_capability_snapshot(path)
    assert snapshot is not None
    assert snapshot["capabilities"][0]["available"] is True


def test_capability_snapshot_must_match_live_process_revision_and_connection_time():
    snapshot = {
        "schema_version": 1,
        "process_id": 123,
        "solidworks_revision": "33.5.0",
        "detected_at_utc": "2026-08-18T01:00:10Z",
        "capabilities": [],
    }
    assert capability_snapshot_matches(
        snapshot,
        process_id=123,
        solidworks_revision="33.5.0",
        connected_at_utc="2026-08-18T01:00:00Z",
    )
    assert not capability_snapshot_matches(snapshot, process_id=456)
    assert not capability_snapshot_matches(
        snapshot, process_id=123, solidworks_revision="34.0.0"
    )
    assert not capability_snapshot_matches(
        snapshot,
        process_id=123,
        connected_at_utc="2026-08-18T02:00:00Z",
    )


def test_context_store_normalizes_capabilities_selection_and_sequence():
    store = SolidWorksContextStore()
    store.set_capabilities(
        {
            "solidworks_revision": "33.5.0",
            "capabilities": [
                {
                    "id": "context.selection",
                    "available": True,
                    "evidence": "member found",
                    "type": "ISelectionMgr",
                    "member": "GetSelectedObject6",
                    "member_kind": "method",
                }
            ],
        }
    )
    value = _event("context", "context.changed")
    value["sequence"] = 9
    value["payload"] = {
        "context": {
            "captured_at": "2099-01-01T00:00:00+00:00",
            "process_id": 456,
            "solidworks_revision": "33.5.0",
            "command_in_progress": False,
            "document": {"title": "测试.SLDPRT", "read_only": False},
            "selections": [
                {
                    "index": 1,
                    "selection_type": 2,
                    "type_name": "face",
                    "name": "Face1",
                    "persistent_ref": "AQID",
                }
            ],
            "stale_after_ms": 15000,
        }
    }
    snapshot = store.update(AddinEvent.from_mapping(value))
    assert snapshot is not None
    assert snapshot.process_id == 456
    assert snapshot.selections[0].type_name == "face"
    assert snapshot.capabilities[0].available is True
    assert snapshot.capabilities[0].evidence["type_name"] == "ISelectionMgr"
    assert store.update(AddinEvent.from_mapping(value)) is None
    assert store.latest(require_fresh=True) is snapshot
