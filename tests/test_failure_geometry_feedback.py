"""Synthetic readback evidence survives candidate restoration and both agent routes."""
from __future__ import annotations

import copy
import json

from sw_assistant.agent.diagnostics import inspect_recent_failures
from sw_assistant.agent.failure_evidence import failure_geometry_summary
from sw_assistant.tools import build_tool_registry
from test_continuous_task_runner import act, runner


def failure():
    geometry = {"geometry_complete": True, "segments": [{
        "type": 0, "construction": False, "start_sketch_m": [-0.012, 0.0, 0.0],
        "end_sketch_m": [0.012, 0.0, 0.0], "persist_reference": "private-native-reference",
    }]}
    state = {"active_sketch": "SyntheticSketch", "sketch_entity_count": 1,
             "active_sketch_geometry": geometry, "active_sketch_geometry_complete": True}
    return {"call": {"tool": "sketch.line", "arguments": {"x1_mm": 12, "y1_mm": -45,
                                                               "x2_mm": 12, "y2_mm": -40}},
            "fingerprint": "synthetic-failure", "state": copy.deepcopy(state),
            "observation": {"snapshot": {"active_sketch": "SyntheticSketch", "sketch_entity_count": 1},
                            "active_sketch_geometry": geometry, "active_sketch_geometry_complete": True},
            "result": {"ok": False, "error_code": "ToolError", "message": "synthetic line failed"}}


def test_failed_geometry_context_does_not_become_restored_baseline(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("Create synthetic part")
    entry = failure()
    task._task["failure_attempts"] = [entry]
    baseline = {"snapshot": {"active_sketch": "", "sketch_entity_count": 0}}
    task._task["latest_observation"] = baseline
    task._task["expected_observation"] = copy.deepcopy(baseline)
    task._task["trial_history"] = [{"status": "restored", "finish_receipt": {"ok": True, "status": "restored"}}]
    context = task._context()
    summary = context["failure_attempts"][0]["geometry_evidence"]
    assert summary["scope"] == "historical_failed_candidate_not_current_state"
    for when in ("before", "after"):
        actual = summary[when]["sketch_geometry"]
        assert actual["coordinate_space"] == "actual_sketch_local"
        assert actual["segments"][0]["end_sketch_m"] == [0.012, 0, 0]
        assert actual["source_complete"] and not actual["truncated"]
    assert context["trial_history"][0]["finish_receipt"]["status"] == "restored"
    assert context["latest_observation"] == baseline
    summary["after"]["sketch_geometry"]["segments"][0]["end_sketch_m"][1] = 999
    assert entry["observation"]["active_sketch_geometry"]["segments"][0]["end_sketch_m"][1] == 0
    assert task._task["expected_observation"] == baseline


def test_native_stage_feedback_preserves_failure_readback_without_reusing_old_failure(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("Create synthetic part")
    task._task["failure_attempts"] = [failure()]
    result = task._native_stage_result("synthetic", task._epoch, 0, 0, 0, 0, False, 0, 0)
    assert result["failure_evidence"][0]["after"]["geometry_available"]
    later = task._native_stage_result("later", task._epoch, 0, 0, 0, 0, False, 0, 1)
    assert later["failure_evidence"] == []


def test_diagnostics_show_failed_geometry_not_only_latest_restored_snapshot():
    result = inspect_recent_failures({"failure_attempts": [failure()]}, build_tool_registry())
    assert result["complete"]
    evidence = result["failures"][0]["geometry_evidence"]
    assert evidence["after"]["sketch_geometry"]["segment_count"] == 1


def test_oversized_native_trial_keeps_finish_receipt_and_failure_geometry(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("Create synthetic part")
    task._task["failure_attempts"] = [failure()]
    task._task["trial_history"] = [{"status": "restored", "checks": ["x" * 60000],
                                  "finish_receipt": {"ok": True, "status": "restored"}}]
    result = task._native_stage_result("synthetic", task._epoch, 0, 0, 0, 0, False, 0, 0)
    assert result["trial"]["details_omitted"]
    assert result["trial"]["finish_receipt"] == {"ok": True, "status": "restored"}
    assert result["failure_evidence"][0]["after"]["geometry_available"]


def test_geometry_summary_is_bounded_scrubbed_and_explicitly_partial():
    entry = failure()
    geometry = entry["observation"]["active_sketch_geometry"]
    segment = geometry["segments"][0]
    geometry["segments"] = [copy.deepcopy(segment) for _ in range(1000)]
    geometry["segments"][-1]["end_sketch_m"] = [1, 2, 3]
    entry["observation"]["snapshot"]["document_path"] = "C:/private/part.sldprt"
    entry["observation"]["api_key"] = "private-key"
    summary = failure_geometry_summary(entry)
    actual = summary["after"]["sketch_geometry"]
    assert actual["source_complete"] and actual["truncated"]
    assert actual["segment_count"] == 1000 and len(actual["segments"]) == 8
    assert actual["segments"][-1]["end_sketch_m"] == [1, 2, 3]
    encoded = json.dumps(summary, allow_nan=False)
    assert "private" not in encoded and "C:/" not in encoded
    assert len(encoded.encode()) < 8000


def test_missing_or_invalid_geometry_is_not_fabricated():
    assert not failure_geometry_summary({})["after"]["available"]
    entry = failure()
    observation = entry["observation"]
    observation["active_sketch_geometry_complete"] = False
    observation["active_sketch_geometry"]["segments"][0].update(
        start_sketch_m=[float("nan"), 0, 0], end_sketch_m=[10 ** 1000, 0, 0], radius_mm=float("inf"))
    actual = failure_geometry_summary(entry)["after"]["sketch_geometry"]
    assert not actual["source_complete"]
    assert "start_sketch_m" not in actual["segments"][0]
    assert "end_sketch_m" not in actual["segments"][0]
    json.dumps(actual, allow_nan=False)


def test_worker_preserves_sketch_creation_error_and_context_retains_safe_receipt(monkeypatch):
    from sw_assistant.solidworks.gateway import SketchCreationError
    from test_worker_preconditions import observation, roundtrip

    evidence = {"kind": "sketch_creation", "mutation_attempted": True, "geometry_verified": False,
                "rollback_verified": False, "requires_review": True, "verification_error": "wrong endpoints",
                "requested_points_m": [[0.012, 0, 0.045]], "api_points_m": [[0.012, -0.045, 0]],
                "unexpected_file_path": "C:/private/part.sldprt"}
    evidence["actual_geometry"] = [{"type": 0, "construction": False,
                                    "start_sketch_m": [0.012, 0, 0], "end_sketch_m": [0.024, 0, 0],
                                    "persist_reference": "private-reference"}]
    result, events = roundtrip(monkeypatch, observation(),
                              execute_error=SketchCreationError("synthetic readback mismatch", evidence))
    assert events.count("execute") == 1 and not result["ok"]
    assert result["error_code"] == "SketchCreationError"
    assert result["data"]["mutation"] == evidence
    summary = failure_geometry_summary({"result": result})
    receipt = summary["creation_verification"]
    assert receipt["requested_points_m"] == [[0.012, 0, 0.045]]
    assert receipt["api_points_m"] == [[0.012, -0.045, 0]]
    assert receipt["requires_review"] and not receipt["geometry_verified"]
    assert receipt["new_entities"]["segments"][0]["end_sketch_m"] == [0.024, 0, 0]
    assert receipt["new_entities"]["scope"] == "newly_added_entities_in_failed_creation"
    assert "private" not in json.dumps(summary)
