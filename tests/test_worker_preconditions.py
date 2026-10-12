"""Host-only, in-gate execution preconditions; no COM or external models."""
from __future__ import annotations

import contextlib
import copy
import queue
from types import SimpleNamespace

import pytest

import sw_assistant.solidworks.worker as worker_module
from sw_assistant.domain import ModelSnapshot, ToolCall


def observation(identity="native:candidate"):
    return {"complete": True, "document_identity": identity,
            "snapshot": ModelSnapshot(connected=True, document_title="Same name",
                                      document_path="", document_type="part",
                                      feature_count=5, body_count=1, volume_m3=7e-7).to_dict(),
            "configuration": "Default", "configuration_identity": "native:config",
            "model_update_stamp": 25}


def roundtrip(monkeypatch, expected, *, current=None, before=None, changed_header=None,
              request_changes=None, execute_error=None, broken_member=None, snapshot_error=None,
              header_error=None, required_identity=None):
    current = copy.deepcopy(current or observation())
    events = []
    depth = 0
    probes = 0

    @contextlib.contextmanager
    def gate(timeout):
        nonlocal depth
        assert timeout == 120 and depth == 0
        depth = 1
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
            depth = 0

    class Gateway:
        def __init__(self, *, visible):
            self.config = SimpleNamespace(Name=current.get("configuration"), identity=current.get("configuration_identity"))
            self.doc = SimpleNamespace(ConfigurationManager=SimpleNamespace(ActiveConfiguration=self.config),
                                       GetUpdateStamp=lambda: current.get("model_update_stamp"),
                                       identity=required_identity or current.get("document_identity"))
            if broken_member:
                delattr(self.doc, broken_member)

        def snapshot(self):
            assert depth == 1
            events.append("snapshot")
            if snapshot_error:
                raise snapshot_error
            return copy.deepcopy(before) if before is not None else ModelSnapshot.from_mapping(current["snapshot"])

        def probe_document_identity(self):
            nonlocal probes
            assert depth == 1
            events.append("header")
            if header_error:
                raise header_error
            probes += 1
            active = changed_header if probes > 1 and changed_header is not None else current
            return {"document_identity": active.get("document_identity"), **{
                key: active["snapshot"][key] for key in
                ("connected", "document_title", "document_path", "document_type")}}

        def require_document(self):
            assert depth == 1
            events.append("document")
            return self.doc

        def _com_identity(self, obj):
            assert depth == 1
            return obj.identity

        def execute(self, call):
            assert depth == 1
            events.append("execute")
            if execute_error:
                raise execute_error
            return "synthetic write", {}

        def release(self, *, close_owned):
            assert depth == 0 and close_owned
            events.append("release")

    monkeypatch.setattr(worker_module, "SolidWorksGateway", Gateway)
    monkeypatch.setattr(worker_module, "solidworks_serial_gate", gate)
    request = {"kind": "checked_call", "id": "checked-1", "expected": expected,
               "call": ToolCall("sketch.begin", {"plane": "front"}).to_dict()}
    for key, value in (request_changes or {}).items():
        if value is ...:
            request.pop(key, None)
        else:
            request[key] = value
    requests, responses = queue.Queue(), queue.Queue()
    requests.put(request)
    requests.put({"kind": "shutdown"})
    worker_module.worker_main(requests, responses)
    return responses.get_nowait()["result"], events


def test_expected_old_document_cannot_write_to_current_candidate(monkeypatch):
    result, events = roundtrip(monkeypatch, observation("native:old-document"))
    assert "execute" not in events
    assert result["ok"] is False and result["error_code"] == "StateChanged"
    assert result["data"]["execution"] == {"started": False, "phase": "precondition"}
    assert "document_identity" in result["data"]["precondition"]["differences"]


def assert_refused(result, events, field=None):
    assert "execute" not in events
    assert result["ok"] is False and result["error_code"] == "StateChanged"
    assert result["data"]["execution"] == {"started": False, "phase": "precondition"}
    if field:
        assert field in result["data"]["precondition"]["differences"]


def test_before_source_then_header_back_to_candidate_is_still_refused(monkeypatch):
    before = ModelSnapshot.from_mapping(observation()["snapshot"])
    before.document_title = "Original source"
    before.document_path = "E:\\synthetic\\source.SLDPRT"
    result, events = roundtrip(monkeypatch, observation(), before=before)
    assert_refused(result, events, "document_title")


def test_stable_candidate_writes_once_inside_same_gate_and_keeps_after_snapshot(monkeypatch):
    result, events = roundtrip(monkeypatch, observation())
    assert result["ok"] is True
    assert events == ["enter", "snapshot", "header", "document", "header", "execute", "snapshot", "exit", "release"]
    assert result["before"] == result["after"]


@pytest.mark.parametrize("key,value", [
    ("configuration", "Other"), ("configuration_identity", "native:other-config"),
    ("model_update_stamp", 26),
])
def test_native_config_and_stamp_changes_prevent_execution(monkeypatch, key, value):
    current = observation()
    current[key] = value
    result, events = roundtrip(monkeypatch, observation(), current=current)
    assert_refused(result, events, key)


def test_second_header_detects_switch_during_configuration_reads(monkeypatch):
    result, events = roundtrip(monkeypatch, observation(), changed_header=observation("native:other-document"))
    assert_refused(result, events, "document_identity")
    assert events.count("header") == 2


@pytest.mark.parametrize("field,value", [
    ("connected", False), ("connected", 1), ("document_title", ""), ("document_title", 5),
    ("document_path", None), ("document_type", "unknown"), ("document_type", []),
    ("active_sketch", None), ("body_count", True), ("body_count", -1),
    ("feature_count", 5.0), ("sketch_entity_count", "0"), ("selection_count", None),
    ("volume_m3", float("nan")), ("volume_m3", float("inf")), ("volume_m3", float("-inf")),
    ("volume_m3", -1), ("volume_m3", True), ("volume_m3", "7e-7"), ("volume_m3", 10**500),
])
def test_invalid_expected_snapshot_cannot_be_used_as_authority(monkeypatch, field, value):
    expected = observation()
    expected["snapshot"][field] = value
    result, events = roundtrip(monkeypatch, expected)
    assert_refused(result, events, f"expected.snapshot.{field}")


@pytest.mark.parametrize("field", [
    "connected", "document_title", "document_path", "document_type", "active_sketch",
    "sketch_entity_count", "feature_count", "body_count", "volume_m3", "selection_count",
])
def test_each_expected_snapshot_field_is_required(monkeypatch, field):
    expected = observation()
    del expected["snapshot"][field]
    result, events = roundtrip(monkeypatch, expected)
    assert_refused(result, events, f"expected.snapshot.{field}")


@pytest.mark.parametrize("expected", [None, {}, [], "observation", {"complete": 1}, {"complete": False}])
def test_missing_or_invalid_expected_never_falls_through_to_execute(monkeypatch, expected):
    result, events = roundtrip(monkeypatch, expected)
    assert_refused(result, events)


@pytest.mark.parametrize("changes", [{"expected": ...}, {"path": "untrusted"}, {"workspace": "untrusted"}])
def test_exact_checked_rpc_fields_are_required(monkeypatch, changes):
    result, events = roundtrip(monkeypatch, observation(), request_changes=changes)
    assert_refused(result, events, "request")
    assert events == ["release"]


@pytest.mark.parametrize("field,value", [
    ("configuration", None), ("configuration", 5), ("configuration_identity", ""),
    ("configuration_identity", False), ("model_update_stamp", "25"),
    ("model_update_stamp", -1), ("model_update_stamp", True), ("model_update_stamp", None),
    ("document_identity", None), ("document_identity", 5),
])
def test_optional_expected_native_evidence_when_present_must_be_valid(monkeypatch, field, value):
    expected = observation()
    expected[field] = value
    result, events = roundtrip(monkeypatch, expected)
    assert_refused(result, events, field)


@pytest.mark.parametrize("member", ["ConfigurationManager", "GetUpdateStamp"])
def test_missing_native_configuration_or_stamp_fails_closed(monkeypatch, member):
    result, events = roundtrip(monkeypatch, observation(), broken_member=member)
    assert_refused(result, events, "native_state")


def test_without_optional_fields_checks_document_and_snapshot(monkeypatch):
    expected = observation()
    for key in ("configuration", "configuration_identity", "model_update_stamp"):
        del expected[key]
    result, events = roundtrip(monkeypatch, expected)
    assert result["ok"] is True and events.count("execute") == 1


@pytest.mark.parametrize("field,value", [
    ("volume_m3", float("nan")), ("volume_m3", float("inf")),
    ("volume_m3", None), ("body_count", False), ("selection_count", "0"),
])
def test_current_snapshot_unknown_or_nonfinite_values_are_not_proof(monkeypatch, field, value):
    before = ModelSnapshot.from_mapping(observation()["snapshot"])
    setattr(before, field, value)
    result, events = roundtrip(monkeypatch, observation(), before=before)
    assert_refused(result, events, f"before.{field}")


@pytest.mark.parametrize("field,value", [
    ("document_title", "Other title"), ("document_path", "E:\\synthetic\\other.SLDPRT"),
    ("document_type", "assembly"), ("active_sketch", "Sketch2"),
    ("sketch_entity_count", 1), ("feature_count", 6), ("body_count", 2),
    ("volume_m3", 1e-6), ("selection_count", 1),
])
def test_every_changed_snapshot_state_refuses_before_execute(monkeypatch, field, value):
    before = ModelSnapshot.from_mapping(observation()["snapshot"])
    setattr(before, field, value)
    result, events = roundtrip(monkeypatch, observation(), before=before)
    assert_refused(result, events, field)


def test_volume_noise_uses_same_tolerance_as_runner(monkeypatch):
    before = ModelSnapshot.from_mapping(observation()["snapshot"])
    before.volume_m3 += 1e-13
    result, _ = roundtrip(monkeypatch, observation(), before=before)
    assert result["ok"] is True


def test_execute_failure_is_not_falsely_reported_as_unstarted(monkeypatch):
    from sw_assistant.solidworks.preconditions import StateChanged

    result, events = roundtrip(monkeypatch, observation(), execute_error=StateChanged("inside execute", ["native_state"]))
    assert result["ok"] is False and events.count("execute") == 1
    assert result["data"]["execution"] == {"started": True, "phase": "executing"}
    assert result["after"] is not None


def test_precondition_snapshot_fields_track_runner_contract():
    from sw_assistant.agent.task_runner import _SNAPSHOT_FIELDS
    from sw_assistant.solidworks.preconditions import SNAPSHOT_FIELDS

    assert SNAPSHOT_FIELDS == (*_SNAPSHOT_FIELDS, "selection_count")


@pytest.mark.parametrize("kind", ["call", None, ..., "checked_cal", "execute", "", 123])
def test_expected_authority_cannot_be_downgraded_to_unchecked_call(monkeypatch, kind):
    result, events = roundtrip(monkeypatch, observation(), request_changes={"kind": kind})
    assert_refused(result, events, "request.kind")
    assert events == ["release"]


@pytest.mark.parametrize("kind", ["unexpected", "execute", "", 123, [], {}])
def test_unknown_request_kind_is_rejected_without_expected_too(monkeypatch, kind):
    result, events = roundtrip(monkeypatch, observation(), request_changes={"kind": kind, "expected": ...})
    assert_refused(result, events, "request.kind")
    assert events == ["release"]


@pytest.mark.parametrize("kind", ["call", None, ...])
def test_legacy_ordinary_calls_remain_compatible_without_expected(monkeypatch, kind):
    result, events = roundtrip(monkeypatch, observation(), request_changes={"kind": kind, "expected": ...})
    assert result["ok"] is True and events.count("execute") == 1
    assert "header" not in events


@pytest.mark.parametrize("issues", [["GetBodies2 unavailable"], "", {}, None, False])
def test_declared_complete_observation_with_issues_is_not_authority(monkeypatch, issues):
    expected = observation()
    expected["issues"] = issues
    result, events = roundtrip(monkeypatch, expected)
    assert_refused(result, events, "expected.issues")


def test_explicit_empty_observation_issues_are_valid(monkeypatch):
    expected = observation()
    expected["issues"] = []
    result, _ = roundtrip(monkeypatch, expected)
    assert result["ok"] is True


def test_broken_native_snapshot_refuses_checked_call_with_unstarted_evidence(monkeypatch):
    result, events = roundtrip(monkeypatch, observation(), snapshot_error=RuntimeError("native read failed"))
    assert_refused(result, events, "before")


def test_unreadable_native_header_fails_closed(monkeypatch):
    result, events = roundtrip(monkeypatch, observation(), header_error=RuntimeError("header read unavailable"))
    assert_refused(result, events, "document_identity")


def test_document_switch_between_header_and_native_document_read_fails_closed(monkeypatch):
    result, events = roundtrip(monkeypatch, observation(), required_identity="native:other-document")
    assert_refused(result, events, "document_identity")


@pytest.mark.parametrize("key,value", [
    ("configuration", None), ("configuration_identity", None),
    ("model_update_stamp", True), ("model_update_stamp", "25"), ("model_update_stamp", 25.0),
    ("model_update_stamp", None), ("model_update_stamp", -1),
])
def test_invalid_native_optional_values_fail_closed(monkeypatch, key, value):
    current = observation()
    current[key] = value
    result, events = roundtrip(monkeypatch, observation(), current=current)
    assert_refused(result, events, key)
