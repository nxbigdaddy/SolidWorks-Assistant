"""Privacy/ownership checks for opt-in configured-model tests; no network or COM."""
from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from sw_assistant.domain import ModelSnapshot, StepResult, ToolCall


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_configured_agent_smoke.py"
MODULE = runpy.run_path(str(SCRIPT))


class LocalWorker:
    def __init__(self):
        self.identity = "test-fixture"
        self.path = ""
        self.title = "Synthetic Test"
        self.calls = []

    def probe_document_identity(self):
        return {"document_identity": self.identity, "document_path": self.path,
                "document_title": self.title, "connected": True, "document_type": "part"}

    def call(self, call, timeout_seconds=120):
        self.calls.append(call.tool)
        if call.tool == "document.new_part":
            self.identity = "new-test-fixture"
        return StepResult(True, call, "fixture only", before=ModelSnapshot(document_title=self.title),
                          after=ModelSnapshot(document_title=self.title), data={
            "complete": True, "document_identity": self.identity,
            "snapshot": {"document_path": self.path, "document_title": self.title},
        })


def test_plan_only_worker_never_executes_writes():
    worker = MODULE["SyntheticUnconnectedCad"]()
    for name in ("session.ensure", "document.new_part", "sketch.circle"):
        with pytest.raises(AssertionError, match="cannot execute CAD writes"):
            worker.call(ToolCall(name, {}))
    observed = worker.call(ToolCall("model.observe", {}))
    assert observed.data["snapshot"]["document_type"] == ModelSnapshot().document_type
    assert observed.data["snapshot"]["document_path"] == ""


@pytest.mark.parametrize("changed", ["identity", "saved"])
def test_real_cad_wrapper_never_returns_foreign_document(changed):
    raw = LocalWorker()
    worker = MODULE["OwnedFixtureCad"](raw)
    worker._adopt_new()
    if changed == "identity":
        raw.identity = "private-document"
    else:
        raw.path = "private-file.sldprt"
    with pytest.raises(RuntimeError, match="no user CAD data"):
        worker.call(ToolCall("model.references", {}))
    assert "model.references" not in raw.calls


def test_real_cad_wrapper_adopts_only_new_unsaved_fixture():
    raw = LocalWorker()
    worker = MODULE["OwnedFixtureCad"](raw)
    worker.create()
    assert raw.calls[:2] == ["session.ensure", "document.new_part"]
    assert worker.identity == "new-test-fixture"
    assert worker.call(ToolCall("model.inspect", {})).ok


def test_saved_document_cannot_be_adopted():
    raw = LocalWorker()
    raw.path = "private-file.sldprt"
    worker = MODULE["OwnedFixtureCad"](raw)
    with pytest.raises(RuntimeError, match="could not be identified"):
        worker._adopt_new()


def test_switched_unsaved_document_cannot_be_adopted():
    raw = LocalWorker()
    worker = MODULE["OwnedFixtureCad"](raw)
    with pytest.raises(RuntimeError, match="could not be identified"):
        worker._adopt_new("The actual newly created title")


def test_document_switched_inside_call_withholds_result():
    class SwitchingWorker(LocalWorker):
        def call(self, call, timeout_seconds=120):
            result = super().call(call, timeout_seconds)
            if call.tool == "model.inspect":
                self.identity = "private-doc"
            return result

    raw = SwitchingWorker()
    worker = MODULE["OwnedFixtureCad"](raw)
    worker._adopt_new()
    with pytest.raises(RuntimeError, match="result withheld"):
        worker.call(ToolCall("model.inspect", {}))


def test_identity_guards_do_not_add_full_geometry_observations():
    raw = LocalWorker()
    worker = MODULE["OwnedFixtureCad"](raw)
    worker._adopt_new()
    worker.call(ToolCall("model.inspect", {}))
    assert raw.calls == ["model.inspect"]
    assert len(worker.timings) == 1
    assert worker.timings[0]["tool"] == "model.inspect"


def test_uninitialized_fixture_is_never_authorized():
    raw = LocalWorker()
    raw.identity = None
    worker = MODULE["OwnedFixtureCad"](raw)
    with pytest.raises(RuntimeError, match="has not been established"):
        worker.call(ToolCall("document.new_part", {}))
    assert raw.calls == []


def test_plan_only_extents_cannot_read_any_cad():
    result = MODULE["SyntheticUnconnectedCad"]().call(ToolCall("model.measure_extents", {}))
    assert not result.ok and result.error_code == "NoActiveDocument"


@pytest.mark.parametrize("bad", [None, "volume", "extent", "identity", "incomplete", "approximate", "nan"])
def test_independent_cube_acceptance_requires_actual_geometry(bad):
    observation = {"complete": True, "document_identity": "fixture", "features": [{"error_code": 0}],
                   "snapshot": {"document_type": "part", "document_path": "", "body_count": 1,
                                "active_sketch": "", "volume_mm3": 1000.0}}
    measurement = {"complete": True, "document_identity": "fixture", "bodies_count": 1,
                   "source": "IBody2.GetExtremePoint", "coordinate_system": "model", "issues": [],
                   "extents_mm": {"x": 10.0, "y": 10.0, "z": 10.0}}
    if bad == "volume":
        observation["snapshot"]["volume_mm3"] = 900.0
    elif bad == "extent":
        measurement["extents_mm"] = {"x": 5.0, "y": 10.0, "z": 20.0}
    elif bad == "identity":
        measurement["document_identity"] = "foreign"
    elif bad == "incomplete":
        measurement["complete"] = False
    elif bad == "approximate":
        measurement["source"] = "GetBodyBox"
    elif bad == "nan":
        measurement["extents_mm"]["z"] = float("nan")
    assert MODULE["check_cube_acceptance"](observation, measurement)["passed"] is (bad is None)
