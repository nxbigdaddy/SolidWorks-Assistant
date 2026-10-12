"""Synthetic acceptance regression: no COM, network, or real CAD state."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from sw_assistant.agent.task_backend import INVESTIGATION_TOOLS, SYSTEM_PROMPT
from sw_assistant.agent.task_contract import contract_issues, refine_goals
from sw_assistant.agent.task_models import GoalCheck
from sw_assistant.domain import StepResult
from sw_assistant.tools import ToolSpec

from test_continuous_task_runner import act, complete, goal, runner


def extent(axis="x", **changes):
    return goal(id=f"extent_{axis}", description=f"模型坐标 {axis} 向整体外廓十毫米",
                metric="extent_mm", target=axis, expected=10, **changes)


def measured_task(tmp_path, *, changes=None, decisions=None):
    goals = [goal(), extent("x"), extent("y"), extent("z")]
    task, worker, backend = runner(tmp_path, decisions or [act(goals=goals), complete()])
    if not any(item["name"] == "model.measure_extents" for item in task.registry.describe()):
        task.registry.register(ToolSpec("model.measure_extents", "Synthetic extent read"))
    original = worker.call
    value = {"complete": True, "document_identity": "synthetic-document", "document_title": "Part1",
             "document_path": "", "configuration": "Default", "configuration_identity": "synthetic-config",
             "coordinate_system": "model", "extents_mm": {"x": 10.0, "y": 10.0, "z": 10.0},
             "source": "IBody2.GetExtremePoint", "bodies_count": 1, "issues": []}
    value.update(changes or {})

    def call(tool_call, timeout_seconds=120):
        if tool_call.tool == "model.measure_extents":
            worker.calls.append(tool_call.tool)
            if worker.callback:
                worker.callback(tool_call)
            return StepResult(True, tool_call, "Synthetic exact extent", data=copy.deepcopy(value), duration_ms=123)
        result = original(tool_call, timeout_seconds)
        if tool_call.tool == "model.observe" and result.data["snapshot"]["document_type"] != "none":
            result.data["document_identity"] = "synthetic-document"
            if tool_call.arguments.get("include_configuration"):
                result.data.update(configuration="Default", configuration_identity="synthetic-config")
        return result

    worker.call = call
    return task, worker, backend, value


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_extent_goal_has_fixed_model_axis_contract(axis):
    value = extent(axis)
    assert value.binding == "fixed" and value.target == axis


@pytest.mark.parametrize("changes", [{"target": ""}, {"target": "width"}, {"target": "D1@Boss"},
                                    {"target": "X"}, {"binding": "pending"}, {"expected": True},
                                    {"expected": float("nan")}, {"tolerance": -1}])
def test_extent_goal_rejects_unknown_axes_pending_or_invalid_numeric_contract(changes):
    values = extent().model_dump()
    values.update(changes)
    with pytest.raises(ValidationError):
        GoalCheck.model_validate(values)


def test_distinct_dimension_goals_cannot_bind_one_parameter():
    goals = [goal(id=name, description=name, metric="dimension_mm", expected=10, target="", binding="pending")
             for name in ("sketch_side", "extrusion_depth")]
    observed = {"complete": True, "dimensions": [{"name": "D1@Boss", "value_mm": 10}]}
    proposed = [item.model_copy(update={"target": "D1@Boss", "binding": "fixed"}) for item in goals]
    with pytest.raises(ValueError, match="不能复用同一参数"):
        refine_goals([item.model_dump() for item in goals], proposed, observed)
    issues = contract_issues([{"id": "r1", "goal_ids": [item.id for item in goals]}],
                             [item.model_dump() for item in proposed])
    assert any("不能复用" in issue for issue in issues)


def test_two_requirements_may_share_one_same_physical_goal():
    dimension = goal(id="depth", metric="dimension_mm", expected=10, target="D1@Boss").model_dump()
    assert contract_issues([{"id": "r1", "goal_ids": ["depth"]}, {"id": "r2", "goal_ids": ["depth"]}], [dimension]) == []


def test_independent_extents_cannot_reuse_one_axis():
    width = extent("x")
    height = width.model_copy(update={"id": "height", "description": "height"})
    with pytest.raises(ValueError, match="不能复用同一参数或外廓轴"):
        refine_goals([], [width, height], {})


def test_locked_dimension_cannot_be_replaced_by_extent_or_deleted():
    previous = goal(id="side", metric="dimension_mm", expected=10, target="", binding="pending")
    replacement = extent().model_copy(update={"id": "side"})
    with pytest.raises(ValueError, match="不得改变测量指标"):
        refine_goals([previous.model_dump()], [replacement], {})
    with pytest.raises(ValueError, match="不得删除"):
        refine_goals([previous.model_dump()], [extent()], {})


def test_locked_extent_cannot_change_axis_or_weaken_tolerance():
    previous = extent()
    with pytest.raises(ValueError, match="引用不得改变"):
        refine_goals([previous.model_dump()], [previous.model_copy(update={"target": "y"})], {})
    with pytest.raises(ValueError, match="不得放宽"):
        refine_goals([previous.model_dump()], [previous.model_copy(update={"tolerance": 1000})], {})


def test_extent_verification_is_fresh_and_does_not_spend_agent_read_quota(tmp_path):
    task, worker, backend, _ = measured_task(tmp_path)

    def quota_used():
        if len(backend.contexts) > 1:
            task._task["round_diagnostics"] = 8

    backend.callback = quota_used
    assert task.prepare("创建十毫米正方体")["status"] == "ready"
    assert "model.measure_extents" not in worker.calls
    result = task.run()
    assert result["status"] == "completed", result["message"]
    index = worker.calls.index("model.measure_extents")
    assert worker.calls[index - 1] == worker.calls[index + 1] == "model.observe"
    assert worker.calls.count("model.measure_extents") == 1
    actual = {item["id"]: item["actual"] for item in result["verification"]["goals"]}
    assert actual["extent_x"] == actual["extent_y"] == actual["extent_z"] == 10
    record = next(item for item in result["diagnostic_history"] if item["origin"] == "verification")
    assert record["duration_ms"] == 123


def test_tasks_without_extent_goals_do_not_pay_for_new_measurement(tmp_path):
    task, worker, _, _ = measured_task(tmp_path, decisions=[act(), complete()])
    task.prepare("创建体积一千的零件")
    assert task.run()["status"] == "completed"
    assert "model.measure_extents" not in worker.calls


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"source": "GetBodyBox"}, {"coordinate_system": "sketch"},
    {"document_identity": None}, {"document_identity": "foreign"}, {"document_title": "OtherPart"},
    {"document_path": "C:/synthetic/saved.sldprt"}, {"configuration": ""}, {"configuration_identity": None},
    {"bodies_count": 0}, {"bodies_count": 2}, {"bodies_count": True}, {"issues": ["partial"]},
    {"extents_mm": {"x": 10, "y": None, "z": 10}},
    {"extents_mm": {"x": 10, "y": float("nan"), "z": 10}},
    {"extents_mm": {"x": 10, "y": float("inf"), "z": 10}},
    {"extents_mm": {"x": 10, "y": True, "z": 10}},
    {"extents_mm": {"x": 10, "y": 0, "z": 10}},
])
def test_extent_invalid_evidence_never_completes(tmp_path, changes):
    task, _, _, _ = measured_task(tmp_path, changes=changes)
    task.prepare("创建正方体")
    result = task.run()
    assert result["status"] != "completed"
    assert not result["verification"].get("passed")


def test_wrong_extent_cannot_be_hidden_by_correct_volume(tmp_path):
    task, _, _, _ = measured_task(tmp_path, changes={"extents_mm": {"x": 20, "y": 5, "z": 10}})
    task.prepare("创建十毫米正方体")
    result = task.run()
    assert result["status"] != "completed"
    results = {item["id"]: item for item in result["verification"]["goals"]}
    assert results["volume"]["passed"] and not results["extent_x"]["passed"] and not results["extent_y"]["passed"]


def test_stop_during_extent_measurement_discards_evidence(tmp_path):
    task, worker, _, _ = measured_task(tmp_path)
    worker.callback = lambda call: task.stop() if call.tool == "model.measure_extents" else None
    task.prepare("创建正方体")
    result = task.run()
    assert result["status"] == "stopped"
    assert worker.calls[-1] == "model.measure_extents"
    assert not any(item["origin"] == "verification" for item in result["diagnostic_history"])


def test_changed_model_during_extent_measurement_requires_review(tmp_path):
    task, worker, _, _ = measured_task(tmp_path)
    worker.callback = lambda call: setattr(worker.snapshot, "volume_m3", 0.000002) if call.tool == "model.measure_extents" else None
    task.prepare("创建正方体")
    result = task.run()
    assert result["status"] == "review"
    assert "改变" in result["message"]


def test_configuration_changes_even_with_identical_geometry_require_review(tmp_path):
    task, worker, _, _ = measured_task(tmp_path)
    original = worker.call
    measured = False

    def call(tool_call, timeout_seconds=120):
        nonlocal measured
        result = original(tool_call, timeout_seconds)
        if tool_call.tool == "model.measure_extents":
            measured = True
        elif tool_call.tool == "model.observe" and measured:
            result.data["configuration_identity"] = "another-configuration"
        return result

    worker.call = call
    task.prepare("创建正方体")
    result = task.run()
    assert result["status"] == "review"
    assert "配置身份" in result["message"]


def test_missing_configuration_observation_cannot_verify_extents(tmp_path):
    task, worker, _, _ = measured_task(tmp_path)
    original = worker.call

    def call(tool_call, timeout_seconds=120):
        result = original(tool_call, timeout_seconds)
        if tool_call.tool == "model.observe":
            result.data.pop("configuration_identity", None)
        return result

    worker.call = call
    task.prepare("创建正方体")
    result = task.run()
    assert result["status"] == "review"
    assert "配置身份" in result["message"]


def test_extent_preflight_refusal_never_calls_worker(tmp_path):
    task, worker, _, _ = measured_task(tmp_path)
    task.preflight = lambda call: (False, "measurement permission denied") if call.tool == "model.measure_extents" else (True, "")
    task.prepare("创建正方体")
    result = task.run()
    assert result["status"] == "review" and result["review_kind"] == "safety"
    assert "model.measure_extents" not in worker.calls


def test_old_pending_sketch_goal_remains_unfinished_with_explicit_capability_message(tmp_path):
    side = goal(id="side", description="草图边长", metric="dimension_mm", expected=10, target="", binding="pending")
    task, _, _ = runner(tmp_path, [act(goals=[goal(), side]), complete(), complete(), complete()])
    task.prepare("创建十毫米正方体")
    result = task.run()
    assert result["status"] == "review" and result["review_kind"] == "capability"
    assert "side（草图边长）" in result["message"] and "不能用拉伸深度替代" in result["message"]
    assert result["goals"][1] == side.model_dump(mode="json")


def test_extent_tool_is_exposed_readonly_and_prompt_explains_metric_limits(tmp_path):
    task, worker, backend, _ = measured_task(tmp_path)
    backend.callback = lambda: task.investigate("model.measure_extents", {})
    task.prepare("创建正方体")
    assert worker.calls == ["model.measure_extents"]
    assert "model.measure_extents" in INVESTIGATION_TOOLS
    assert "不自动添加命名尺寸" in SYSTEM_PROMPT
    assert "不能把拉伸深度" in SYSTEM_PROMPT
    assert "外廓只证明整体尺寸" in SYSTEM_PROMPT
