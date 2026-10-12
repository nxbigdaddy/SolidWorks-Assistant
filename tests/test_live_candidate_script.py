"""Offline tests only: no COM imports, SolidWorks processes, CAD files or models."""
from __future__ import annotations

import copy
import json
import math
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import live_candidate_trial_smoke as smoke
from sw_assistant.domain import ToolCall, ToolError


def context():
    return {"requirements": [{"id": "r1"}], "completed_steps": [], "trial_history": []}


def test_fixture_backend_has_exactly_five_bounded_decisions_and_fixed_final_goals():
    backend, data = smoke.FixtureBackend(), context()
    first = backend.decide(data)
    assert first.action == "act" and first.trial is None
    assert [item.tool for item in first.steps] == ["document.new_part"]
    assert first.requirements[0].goal_ids == [item.id for item in first.goals]
    assert next(item.expected for item in first.goals if item.metric == "volume_mm3") == smoke.FINAL_VOLUME
    assert [(item.target, item.expected) for item in first.goals if item.metric == "extent_mm"] == [
        ("x", 7), ("y", 9), ("z", 11),
    ]
    data["completed_steps"] = [{"tool": "document.new_part"}]
    wrong = backend.decide(data)
    assert wrong.steps[1].arguments == {"width_mm": 10, "height_mm": 9}
    assert wrong.trial.checks[0].expected == 693
    assert not wrong.goals
    data["trial_history"] = [{"status": "restored"}]
    repaired = backend.decide(data)
    assert repaired.steps[1].arguments == {"width_mm": 7, "height_mm": 9}
    assert repaired.trial.checks[0].expected == wrong.trial.checks[0].expected
    data["trial_history"].append({"status": "committed"})
    cut = backend.decide(data)
    assert cut.steps[1].arguments == {"radius_mm": 1}
    assert cut.steps[2].arguments == {"depth_mm": 5, "operation": "cut", "reverse": True}
    assert cut.trial.checks[0].expected == smoke.FINAL_VOLUME
    data["trial_history"].append({"status": "committed"})
    assert backend.decide(data).action == "complete"
    exhausted = backend.decide(data)
    assert exhausted.action == "review" and exhausted.review_critical
    assert backend.rounds == 6


@pytest.mark.parametrize("history", [["committed"], ["restored", "restored"], ["starting"]])
def test_fixture_backend_stops_when_feedback_differs_from_expected(history):
    backend, data = smoke.FixtureBackend(), context()
    backend.decide(data)
    data["completed_steps"] = [{"tool": "document.new_part"}]
    data["trial_history"] = [{"status": value} for value in history]
    decision = backend.decide(data)
    assert decision.action == "review" and decision.review_kind == "safety"
    assert not decision.steps


def test_fixture_backend_never_builds_without_verified_initialization():
    backend, data = smoke.FixtureBackend(), context()
    backend.decide(data)
    assert backend.decide(data).action == "review"


def evidence():
    identity = {"document_identity": "test-part", "configuration": "Default",
                "configuration_identity": "test-configuration"}
    observation = {**identity, "complete": True, "snapshot": {
        "volume_m3": smoke.FINAL_VOLUME / 1e9, "body_count": 1, "active_sketch": "",
    }, "features": [{"name": "TestBoss", "error_code": 0}, {"name": "TestCut", "error_code": 0}]}
    measurement = {**identity, "complete": True, "issues": [], "source": "IBody2.GetExtremePoint",
                   "coordinate_system": "model", "bodies_count": 1,
                   "extents_mm": {"x": 7.0, "y": 9.0, "z": 11.0}}
    markers = [{**identity, "update_stamp": 123} for _ in range(4)]
    return observation, measurement, copy.deepcopy(observation), markers


def test_independent_acceptance_passes_only_fresh_matching_geometry():
    before, measurement, after, markers = evidence()
    result = smoke.independent_acceptance(before, measurement, after, markers=markers)
    assert result["passed"] and all(result["checks"].values())
    assert result["update_stamps"] == [123] * 4
    assert result["volume_mm3"] == pytest.approx(693 - 5 * math.pi)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("stamp", [124, None, -1, 123.0, True, "123", math.nan])
def test_stamp_mismatch_or_unknown_rejects_even_when_shape_and_volume_match(index, stamp):
    before, measurement, after, markers = evidence()
    markers[index]["update_stamp"] = stamp
    result = smoke.independent_acceptance(before, measurement, after, markers=markers)
    assert not result["passed"] and not result["checks"]["stable_update_stamp"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("count", [0, 1, 3, 5])
def test_independent_acceptance_requires_all_four_markers(count):
    before, measurement, after, markers = evidence()
    markers = (markers * 2)[:count]
    assert not smoke.independent_acceptance(before, measurement, after, markers=markers)["passed"]


@pytest.mark.parametrize("markers", [None, {}, "unavailable"])
def test_missing_marker_collection_fails_closed(markers):
    before, measurement, after, _ = evidence()
    result = smoke.independent_acceptance(before, measurement, after, markers=markers)
    assert not result["passed"] and result["update_stamps"] == []


@pytest.mark.parametrize("field", ["document_identity", "configuration", "configuration_identity"])
@pytest.mark.parametrize("where", ["measurement", "after", "marker"])
def test_identity_or_configuration_race_rejects_independent_acceptance(field, where):
    before, measurement, after, markers = evidence()
    target = {"measurement": measurement, "after": after, "marker": markers[2]}[where]
    target[field] = "different"
    assert not smoke.independent_acceptance(before, measurement, after, markers=markers)["passed"]


@pytest.mark.parametrize("failure", ["volume", "nan", "body", "bool_body", "extent", "source", "health", "sketch", "incomplete"])
def test_independent_acceptance_rejects_wrong_geometry_or_missing_evidence(failure):
    before, measurement, after, markers = evidence()
    if failure == "volume":
        after["snapshot"]["volume_m3"] = smoke.BASE_VOLUME / 1e9
    elif failure == "nan":
        after["snapshot"]["volume_m3"] = math.nan
    elif failure == "body":
        after["snapshot"]["body_count"] = 2
    elif failure == "bool_body":
        measurement["bodies_count"] = True
    elif failure == "extent":
        measurement["extents_mm"]["x"] = 10
    elif failure == "source":
        measurement["source"] = "GetBox approximate"
    elif failure == "health":
        after["features"][1]["error_code"] = 1
    elif failure == "sketch":
        after["snapshot"]["active_sketch"] = "NotClosed"
    else:
        before["complete"] = False
    result = smoke.independent_acceptance(before, measurement, after, markers=markers)
    assert not result["passed"]
    json.dumps(result, allow_nan=False)


class Document:
    def __init__(self, identity):
        self.identity, self.title, self.path, self.stamp = identity, identity, "", 100
        self.ConfigurationManager = SimpleNamespace(
            ActiveConfiguration=SimpleNamespace(identity=identity + ":config", Name="Default"))

    def GetType(self):
        return 1

    def GetTitle(self):
        return self.title

    def GetPathName(self):
        return self.path

    def GetUpdateStamp(self):
        return self.stamp


class Gateway:
    def __init__(self, active):
        self._sw = SimpleNamespace(ActiveDoc=active)
        self.activations = []

    @staticmethod
    def _com_identity(document):
        return document.identity

    def _candidate_activate(self, document):
        self.activations.append(document.identity)
        self._sw.ActiveDoc = document

    def __getattr__(self, name):
        raise AssertionError(f"Offline guard must not access CAD geometry or mutation: {name}")


def test_original_guard_only_reactivates_proven_owned_document():
    original, owned = Document("private-original"), Document("test-owned")
    gateway = Gateway(original)
    guard = smoke.OriginalDocumentGuard(gateway)
    owned_header = smoke.document_header(gateway, owned)
    gateway._sw.ActiveDoc = owned
    result = guard.restore_if_safe({owned.identity: owned_header})
    assert result["restored_original"] and result["original_unchanged"]
    assert gateway.activations == [original.identity]
    assert original.stamp == 100


@pytest.mark.parametrize("change", ["stamp", "path", "configuration"])
def test_original_guard_never_activates_after_original_changes(change):
    original, owned = Document("private-original"), Document("test-owned")
    gateway = Gateway(original)
    guard = smoke.OriginalDocumentGuard(gateway)
    gateway._sw.ActiveDoc = owned
    if change == "stamp":
        original.stamp += 1
    elif change == "path":
        original.path = "C:/private/changed.sldprt"
    else:
        original.ConfigurationManager.ActiveConfiguration.Name = "Other"
    with pytest.raises(ToolError, match="Original user document changed"):
        guard.restore_if_safe({owned.identity: smoke.document_header(gateway, owned)})
    assert not gateway.activations


def test_original_guard_preserves_a_foreign_active_document():
    original, foreign = Document("private-original"), Document("user-switched-to-this")
    gateway = Gateway(original)
    guard = smoke.OriginalDocumentGuard(gateway)
    gateway._sw.ActiveDoc = foreign
    result = guard.restore_if_safe({"owned": {"identity": "owned"}})
    assert result["reason"] == "active_document_not_owned"
    assert not gateway.activations


def test_no_original_document_requires_no_activation():
    gateway = Gateway(None)
    guard = smoke.OriginalDocumentGuard(gateway)
    gateway._sw.ActiveDoc = Document("test")
    assert guard.restore_if_safe({})["reason"] == "no_original_document"
    assert guard.unchanged() and not gateway.activations


@pytest.mark.parametrize("unchanged", [True, False])
def test_activation_error_does_not_invent_original_document_change(monkeypatch, unchanged):
    calls = []

    class Guard:
        document = object()

        def restore_if_safe(self, owned):
            calls.append("restore")
            raise TypeError("synthetic COM byref argument mismatch")

        def unchanged(self):
            calls.append("verify")
            return unchanged

    monkeypatch.setattr(smoke, "solidworks_serial_gate", lambda *_args: nullcontext())
    result = smoke.restore_original_report(Guard(), {})
    assert result["passed"] is False
    assert result["restore_error_code"] == "TypeError"
    assert "byref" in result["restore_error"]
    assert result["original_unchanged"] is unchanged
    assert result["original_state"] == ("unchanged" if unchanged else "changed")
    assert calls == ["restore", "verify"]  # Verify only; never retry activation.


@pytest.mark.parametrize("unavailable", ["exception", None, "true", 1])
def test_failed_original_readback_is_unknown_not_changed(monkeypatch, unavailable):
    class Guard:
        document = object()

        def restore_if_safe(self, _owned):
            raise TypeError("synthetic activation failure")

        def unchanged(self):
            if unavailable == "exception":
                raise RuntimeError("original object unavailable")
            return unavailable

    monkeypatch.setattr(smoke, "solidworks_serial_gate", lambda *_args: nullcontext())
    result = smoke.restore_original_report(Guard(), {})
    assert result["passed"] is False and result["original_unchanged"] is None
    assert result["original_state"] == "unknown"
    assert result["restore_error_code"] == "TypeError"
    assert result["original_verification_error_code"] in {"RuntimeError", "ToolError"}


def test_successful_restore_report_does_not_promote_a_failed_modeling_test(monkeypatch):
    gateway = Gateway(None)
    guard = smoke.OriginalDocumentGuard(gateway)
    monkeypatch.setattr(smoke, "solidworks_serial_gate", lambda *_args: nullcontext())
    report = {"passed": False}
    report.update(smoke.restore_original_report(guard, {}))
    assert report["passed"] is False
    assert report["original_unchanged"] is True and report["original_state"] == "unchanged"
    assert report["original_document_present"] is False
    assert "restore_error" not in report


def test_initial_observation_is_synthetic_and_cannot_leak_original_geometry(monkeypatch, tmp_path):
    gateway = Gateway(Document("private-original"))
    guard = smoke.OriginalDocumentGuard(gateway)
    worker = smoke.InlineWorker(gateway, guard, tmp_path, time.monotonic() + 60)
    monkeypatch.setattr(smoke, "solidworks_serial_gate", lambda *_args: nullcontext())
    result = worker.call(ToolCall("model.observe", {}))
    assert result.ok and result.data["document_identity"] is None
    assert result.data["snapshot"]["document_title"] == ""
    assert "private-original" not in json.dumps(result.to_dict())
    with pytest.raises(ToolError, match="before accessing CAD geometry"):
        worker.call(ToolCall("model.measure_extents", {}))


def test_update_marker_reads_only_owned_fixture_and_detects_stamp_drift(monkeypatch, tmp_path):
    gateway = Gateway(None)
    guard = smoke.OriginalDocumentGuard(gateway)
    worker = smoke.InlineWorker(gateway, guard, tmp_path, time.monotonic() + 60)
    document = Document("owned")
    gateway._sw.ActiveDoc = document
    worker.owned = {document.identity: smoke.document_header(gateway, document)}
    monkeypatch.setattr(smoke, "solidworks_serial_gate", lambda *_args: nullcontext())
    assert worker.capture_update_marker() == {"document_identity": "owned", "configuration": "Default",
                                               "configuration_identity": "owned:config", "update_stamp": 100}
    stamps = iter([100, 101])
    document.GetUpdateStamp = lambda: next(stamps)
    with pytest.raises(ToolError, match="changed while reading"):
        worker.capture_update_marker()


def test_execute_flag_required_before_any_cad_attachment(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No native gateway may be created without --execute")
    monkeypatch.setattr(smoke, "SolidWorksGateway", forbidden)
    with pytest.raises(SystemExit) as error:
        smoke.main(["--start-solidworks"])
    assert error.value.code == 2
