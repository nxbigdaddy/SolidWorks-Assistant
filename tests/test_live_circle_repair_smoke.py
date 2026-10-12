"""Offline checks of the opt-in live script; these tests never start a COM worker."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from scripts import live_circle_repair_smoke as smoke
from sw_assistant.domain import StepResult


def test_default_preparation_cannot_start_a_worker(monkeypatch, capsys):
    def forbidden():
        raise AssertionError("preparation must not instantiate a worker")
    monkeypatch.setattr(smoke, "SolidWorksWorkerClient", forbidden)
    assert smoke.main([]) == 0
    assert "prepared_not_executed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        smoke.main(["--execute"])


def test_existing_fixture_checkpoint_rejects_user_active_and_changed_state():
    expected = {"document_title": "SyntheticFixture", "document_path": "", "document_type": "part",
                "active_sketch": "Sketch2", "sketch_entity_count": 3, "body_count": 1,
                "feature_count": 8, "volume_m3": 0.00001}
    assert smoke.matches_fixture_checkpoint(expected, copy.deepcopy(expected))
    for field, value in (("document_title", "UserPart"), ("document_path", "C:/user.sldprt"),
                         ("sketch_entity_count", 4), ("body_count", 2), ("volume_m3", 0.1)):
        assert not smoke.matches_fixture_checkpoint(expected, {**expected, field: value})


def test_blank_agent_checkpoint_requires_local_synthetic_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    root = tmp_path / "artifacts" / "configured-agent-smoke" / "cube-test"
    tasks = root / "tasks"
    tasks.mkdir(parents=True)
    summary = {"real_cad": True, "synthetic_data_only": True, "cad_files_saved": 0,
               "scenario": "cube", "status": "stopped", "completed_steps": 0}
    snapshot = {"document_title": "SyntheticFixture", "document_path": "", "document_type": "part",
                "active_sketch": "", "sketch_entity_count": 0, "body_count": 0, "volume_m3": 0.0}
    report = {"latest_observation": {"complete": True, "document_identity": "private-host-token",
                                     "snapshot": snapshot}}
    path = tasks / "task.json"
    (root / "result.json").write_text(json.dumps(summary), encoding="utf-8")
    path.write_text(json.dumps(report), encoding="utf-8")
    assert smoke.load_fixture_checkpoint(path) == snapshot
    for field, value in (("synthetic_data_only", False), ("cad_files_saved", 1), ("completed_steps", 1)):
        (root / "result.json").write_text(json.dumps({**summary, field: value}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="authorized stopped synthetic"):
            smoke.load_fixture_checkpoint(path)
    (root / "result.json").write_text(json.dumps(summary), encoding="utf-8")
    for field, value in (("document_path", "C:/existing.sldprt"), ("body_count", 1), ("active_sketch", "Sketch1")):
        changed = copy.deepcopy(report)
        changed["latest_observation"]["snapshot"][field] = value
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(RuntimeError):
            smoke.load_fixture_checkpoint(path)


class FakeWorker:
    def __init__(self, *, dirty_failure=False, unknown_identity=False, unowned=False):
        self.calls = []
        self.extrusions = 0
        self.circles = 0
        self.repaired = False
        self.dirty_failure, self.unknown_identity, self.unowned = dirty_failure, unknown_identity, unowned

    def observation(self):
        active = self.circles > 0 and self.extrusions < 3
        segments = [{"entity_ref": str(index) * 64, "radius_mm": 7.5 if index == 2 and self.repaired else 8,
                     "editable": True, "sketch_ref": "sketch"} for index in range(1, self.circles + 1)]
        return {"complete": True, "issues": [], "document_identity": None if self.unknown_identity else "fixture-id",
                "features": [{"name": "base", "type": "Boss", "error_code": 0}], "dimensions": [],
                "active_sketch_geometry_complete": True,
                "active_sketch_geometry": {"segments": segments} if active else [],
                "snapshot": {"document_title": "UnsavedFixture", "document_path": "", "document_type": "part",
                             "active_sketch": "Sketch2" if active else "", "sketch_entity_count": self.circles if active else 0,
                             "feature_count": 9 if self.dirty_failure and self.extrusions == 2 else 8,
                             "body_count": 1, "volume_m3": smoke.EXPECTED_VOLUME_MM3 / 1e9}}

    def call(self, call, timeout):
        assert timeout == 180
        self.calls.append(call.tool)
        data = {}
        if call.tool == "sketch.circle":
            self.circles += 1
            data = {"applied": {"creation_verified": not self.unowned, "entity_ref": str(self.circles) * 64}}
        elif call.tool == "feature.extrude":
            self.extrusions += 1
            if self.extrusions == 2:
                return StepResult(False, call, "profile12", error_code="ToolError")
        elif call.tool == "model.observe":
            data = self.observation()
        elif call.tool == "sketch.inspect":
            data = {"complete": True, "sketch": {"geometry_complete": True,
                    "segments": self.observation()["active_sketch_geometry"]["segments"],
                    "profile_checks": [{"usage": "boss-extrude", "status": 12}]}}
        elif call.tool == "sketch.set_circle_radius":
            assert call.arguments == {"entity_ref": "2" * 64, "expected_radius_mm": 8, "radius_mm": 7.5}
            self.repaired = True
            data = {"applied": {"geometry_verified": True}}
        return StepResult(True, call, "synthetic", data=copy.deepcopy(data))


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(smoke, "build_tool_registry", lambda: SimpleNamespace(
        validate=lambda call: call, postcondition=lambda tool: None))


def test_live_script_sequence_uses_expected_failure_and_only_one_bounded_edit(fake_registry):
    worker, report = FakeWorker(), {}
    smoke.run_fixture(worker, report, emit=lambda *_args, **_kwargs: None)
    assert report["status"] == "passed"
    assert all(report["acceptance"]["checks"].values())
    assert worker.calls.count("sketch.set_circle_radius") == 1
    assert worker.calls.count("feature.extrude") == 3
    assert not any("open" in tool or "save" in tool or "delete" in tool for tool in worker.calls)


@pytest.mark.parametrize("option", ["dirty_failure", "unknown_identity", "unowned"])
def test_live_script_stops_before_repair_on_uncertain_fixture_state(fake_registry, option):
    worker, report = FakeWorker(**{option: True}), {}
    with pytest.raises(RuntimeError):
        smoke.run_fixture(worker, report, emit=lambda *_args, **_kwargs: None)
    assert "sketch.set_circle_radius" not in worker.calls
