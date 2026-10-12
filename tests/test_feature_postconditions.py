"""Feature success requires material evidence, not API flags or request echoes."""

from __future__ import annotations

import copy
import math

import pytest

from sw_assistant.domain import ModelSnapshot, StepResult, ToolCall
from sw_assistant.tools import build_tool_registry


def feature_case(tool="feature.extrude", *, operation="boss", merge=True):
    registry = build_tool_registry()
    arguments = {"operation": operation, "merge": merge}
    if tool == "feature.extrude":
        arguments["depth_mm"] = 10
    else:
        arguments["angle_deg"] = 90
    call = registry.validate(ToolCall(tool, arguments))
    applied = {"tool": tool, "operation": operation, "merge": merge, "reverse": False}
    if tool == "feature.extrude":
        applied.update(depth_m=0.01, end_condition="blind")
    else:
        applied["angle_radians"] = math.pi / 2
    before = ModelSnapshot(document_title="Part1", document_type="part", active_sketch="Sketch1",
                           feature_count=3, body_count=1, volume_m3=1e-6)
    after = ModelSnapshot(document_title="Part1", document_type="part", feature_count=4,
                          body_count=1, volume_m3=2e-6 if operation == "boss" else 0.5e-6)
    check = registry.postcondition(tool)
    assert check is not None
    return call, {"applied": applied}, before, after, check


@pytest.mark.parametrize("tool", ["feature.extrude", "feature.revolve"])
@pytest.mark.parametrize(("operation", "merge", "counts", "volumes"), [
    ("boss", True, (0, 1), (0.0, 1e-6)),
    ("boss", True, (1, 1), (1e-6, 2e-6)),
    ("boss", True, (2, 1), (1e-6, 2e-6)),  # A bridge legitimately joins solids.
    ("boss", False, (1, 2), (1e-6, 2e-6)),
    ("boss", False, (0, 2), (0.0, 2e-6)),
    ("boss", True, (0, 1), (0.0, 1e-24)),  # No fixed minimum physical volume.
    ("boss", True, (1, 1), (1e-24, 2e-24)),
    ("boss", True, (1, 1), (1e-6, 1e-6 + 1e-14)),  # Small valid feature on a larger part.
    ("cut", True, (1, 1), (1e-6, 0.5e-6)),
    ("cut", True, (1, 2), (1e-6, 0.5e-6)),  # A cut can split a body.
    ("cut", True, (2, 1), (1e-6, 0.5e-6)),
    ("cut", True, (1, 0), (1e-6, 0.0)),
    ("cut", True, (1, 1), (2e-24, 1e-24)),
])
def test_material_change_accepts_valid_topology_and_scale(tool, operation, merge, counts, volumes):
    call, data, before, after, check = feature_case(tool, operation=operation, merge=merge)
    before.body_count, after.body_count = counts
    before.volume_m3, after.volume_m3 = volumes
    assert check(call, data, before, after)[0]


@pytest.mark.parametrize("tool", ["feature.extrude", "feature.revolve"])
@pytest.mark.parametrize(("operation", "merge", "counts", "volumes", "reason"), [
    ("boss", True, (1, 2), (1e-6, 2e-6), "独立实体"),
    ("boss", True, (0, 2), (0.0, 2e-6), "独立实体"),
    ("boss", True, (2, 3), (1e-6, 2e-6), "独立实体"),
    ("boss", False, (2, 1), (1e-6, 2e-6), "丢失"),
    ("boss", True, (1, 1), (1e-6, 0.5e-6), "材料增量"),
    ("boss", False, (1, 2), (1e-6, 1e-6), "材料增量"),
    ("boss", True, (1, 1), (1e-6, math.nextafter(1e-6, math.inf)), "材料增量"),
    ("boss", True, (1, 1), (3.3880002406209305e-6, 3.388000240620929e-6), "材料增量"),
    ("cut", True, (1, 1), (1e-6, 2e-6), "材料减少"),
    ("cut", True, (2, 1), (1e-6, 2e-6), "材料减少"),
    ("cut", True, (1, 1), (1e-6, math.nextafter(1e-6, 0.0)), "材料减少"),
    ("cut", True, (1, 2), (1e-6, 1e-6), "材料减少"),
    ("cut", True, (0, 1), (0.0, 1e-6), "材料减少"),
])
def test_request_parameter_echo_cannot_hide_invalid_material_change(
    tool, operation, merge, counts, volumes, reason,
):
    call, data, before, after, check = feature_case(tool, operation=operation, merge=merge)
    before.body_count, after.body_count = counts
    before.volume_m3, after.volume_m3 = volumes
    ok, message = check(call, data, before, after)
    assert not ok and reason in message


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(("field", "value"), [
    ("body_count", None), ("body_count", -1), ("body_count", True), ("body_count", 1.0),
    ("volume_m3", None), ("volume_m3", math.nan), ("volume_m3", math.inf),
    ("volume_m3", -math.inf), ("volume_m3", -1), ("volume_m3", True),
    ("volume_m3", "0.000001"), ("volume_m3", 10 ** 400),
    ("volume_m3", 0.0), ("body_count", 0),
    ("feature_count", None), ("feature_count", True), ("feature_count", -1),
])
def test_missing_invalid_or_inconsistent_snapshot_values_fail_closed(side, field, value):
    call, data, before, after, check = feature_case()
    setattr(before if side == "before" else after, field, value)
    assert not check(call, data, before, after)[0]


@pytest.mark.parametrize("tool", ["feature.extrude", "feature.revolve"])
@pytest.mark.parametrize(("field", "value"), [
    ("reverse", None), ("reverse", 0), ("reverse", "false"),
    ("merge", None), ("merge", 1), ("merge", "true"),
])
def test_feature_boolean_readback_must_be_an_actual_boolean(tool, field, value):
    call, data, before, after, check = feature_case(tool)
    data["applied"][field] = value
    assert not check(call, data, before, after)[0]


@pytest.mark.parametrize(("tool", "field"), [
    ("feature.extrude", "depth_m"), ("feature.revolve", "angle_radians"),
])
@pytest.mark.parametrize("value", [None, math.nan, math.inf, 0.0])
def test_feature_size_readback_cannot_be_missing_nonfinite_or_wrong(tool, field, value):
    call, data, before, after, check = feature_case(tool)
    data["applied"][field] = value
    assert not check(call, data, before, after)[0]


@pytest.mark.parametrize("missing", ["before", "after"])
def test_feature_requires_both_snapshots(missing):
    call, data, before, after, check = feature_case()
    assert not check(call, data, None if missing == "before" else before,
                     None if missing == "after" else after)[0]


@pytest.mark.parametrize(("field", "value"), [("active_sketch", "Sketch1"), ("feature_count", 3)])
def test_material_change_alone_is_not_a_completed_feature(field, value):
    call, data, before, after, check = feature_case()
    setattr(after, field, value)
    assert not check(call, data, before, after)[0]


@pytest.mark.parametrize("tool", ["feature.extrude", "feature.revolve"])
def test_manual_confirmation_does_not_replace_feature_parameter_evidence(tool):
    call, _, before, after, check = feature_case(tool)
    assert not check(call, {"manual_verification": True}, before, after)[0]


@pytest.mark.parametrize("field", ["document_title", "document_path", "document_type"])
def test_feature_cannot_use_another_documents_material_change(field):
    call, data, before, after, check = feature_case()
    setattr(after, field, "another")
    assert not check(call, data, before, after)[0]


@pytest.mark.parametrize("bad_result", ["detached", "noise"])
def test_unverified_feature_is_not_added_to_agent_success_history(tmp_path, bad_result):
    from test_continuous_task_runner import act, call, complete, runner

    second_stage = act([
        call("sketch.begin", plane="front"), call("sketch.circle", radius_mm=5),
        call("feature.extrude", depth_mm=10),
    ], goals=[])
    task, worker, _ = runner(tmp_path, [act(), second_stage, complete()])
    task.prepare("创建零件")
    assert task.run(single_stage=True)["status"] == "ready"

    def fake_success(tool_call, before):
        after = copy.deepcopy(before)
        after.active_sketch = ""
        after.sketch_entity_count = 0
        after.feature_count += 1
        if bad_result == "detached":
            after.body_count += 1
            after.volume_m3 *= 2
        else:
            after.volume_m3 = math.nextafter(after.volume_m3, math.inf)
        worker.snapshot = after
        data = feature_case()[1]
        return StepResult(True, tool_call, "API returned success", data=data,
                          before=before, after=copy.deepcopy(after))

    worker.fail["feature.extrude"] = fake_success
    result = task.run()
    assert result["status"] != "completed"
    assert sum(step["tool"] == "feature.extrude" for step in result["completed_steps"]) == 1
    assert result["failure_attempts"][-1]["result"]["error_code"] == "PostconditionFailed"
    assert result["failure_attempts"][-1]["result"]["ok"] is False
