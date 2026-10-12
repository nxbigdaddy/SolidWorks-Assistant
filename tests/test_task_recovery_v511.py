from __future__ import annotations

import copy

import pytest

from sw_assistant.agent.diagnostics import inspect_recent_failures
from sw_assistant.agent.task_contract import (
    contract_issues, is_control_message, merge_requirements, refine_goals, seed_requirements,
)
from sw_assistant.agent.task_models import RequirementCheck, TaskDecision
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.domain import StepResult

from test_continuous_task_runner import Backend, act, call, complete, goal, runner


@pytest.mark.parametrize("text", ["继续", " 请继续！ ", "继续执行", "请继续执行", "resume", "CONTINUE.", "please continue"])
def test_exact_control_never_seeds_geometry_even_when_explicit_requirement(tmp_path, text):
    review = TaskDecision(action="review", reason="核对现场", review_kind="capability", review_critical=True)
    task, _, _ = runner(tmp_path, [review])
    task.prepare("创建连接板")
    original = copy.deepcopy(task.snapshot()["requirements"])
    task.resume(text, as_requirement=True)
    task._consume_steering(task._epoch)
    task.steer(text)
    task._consume_steering(task._epoch)
    assert task.snapshot()["requirements"] == original
    assert all(item["purpose"] == "control" for item in task.snapshot()["messages"][1:])
    assert seed_requirements(text, "u2") == []


@pytest.mark.parametrize("text", ["继续增加 3 个孔", "继续执行，半径 2 mm", "继续并增加凸台", "continue with 3 circles", "请继续上面的 10 毫米要求"])
def test_control_detection_never_swallows_numbers_or_structural_additions(text):
    assert not is_control_message(text)
    assert seed_requirements(text, "u2")[0]["source_text"] == text


def test_legacy_control_migration_preserves_source_history_and_real_constraints(tmp_path):
    source, worker, _ = runner(tmp_path, [act()])
    source.prepare("创建连接板")
    old = source.snapshot()
    old.pop("contract_version")
    for text in ("继续", "继续添加 3 个孔"):
        identifier = f"u{len(old['messages']) + 1}"
        old["messages"].append({"id": identifier, "role": "user", "content": text})
        old["requirements"].append(RequirementCheck(
            id=f"r{len(old['requirements']) + 1}", description=text, source_text=text,
            source_message_id=identifier,
        ).model_dump(mode="json"))
    source.store.save(old)
    restored = ContinuousTaskRunner(source.registry, worker, Backend([]), source.store)
    value = restored.restore(old["id"])
    assert value["messages"] == old["messages"]
    assert value["goals"] == old["goals"]
    assert value["requirements"][1]["source_kind"] == "control"
    assert value["requirements"][2]["source_kind"] == "requirement"
    issues = contract_issues(value["requirements"], value["goals"])
    assert any("r3" in item for item in issues)
    assert not any("r2" in item for item in issues)
    assert any(item["kind"] == "control_source_migration" for item in value["contract_audit"])
    assert restored.restore(old["id"])["contract_audit"] == value["contract_audit"]


def test_agent_cannot_relabel_real_requirement_as_control():
    source = seed_requirements("创建带三个孔的法兰", "initial")
    with pytest.raises(ValueError, match="不得降级"):
        merge_requirements(source, [RequirementCheck(id="r1", source_kind="control")], [], require_complete=False)


def test_repeated_controls_do_not_reopen_a_completed_task(tmp_path):
    task, worker, _ = runner(tmp_path, [act(), complete()])
    task.prepare("创建零件")
    assert task.run()["status"] == "completed"
    calls = list(worker.calls)
    for text in ("继续", "请继续", "resume", "continue"):
        assert task.steer(text)["status"] == "completed"
    assert task.run()["status"] == "completed"
    assert len(task.snapshot()["requirements"]) == 1
    assert worker.calls == calls


def test_host_diagnostics_do_not_consume_strategy_or_agent_investigation_repeat_budget(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("创建零件")
    for index in range(4):
        task._change(task._epoch, rounds=index + 2, round_diagnostics=0)
        task._feedback(task._epoch, "policy_rejected", "同一提案的待绑定引用错误")
        task._feedback(task._epoch, "step_failed", "同一回合已知几何失败，不应双计",
                       state_known=True, result={"error_code": "GeometryError"})
        task._diagnose(task._epoch, "candidate")
    value = task.snapshot()
    assert value["status"] == "ready"
    assert value["no_progress"] == 4
    assert value["diagnostic_no_progress"] == 0
    assert value["diagnostics"] == 8
    assert all(item["origin"] == "host" for item in value["diagnostic_history"])


def test_strategy_limit_review_includes_the_last_actual_policy_error(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("创建零件")
    task._task["failure_budget"]["counts"]["planning"] = 5
    task._change(task._epoch, no_progress=5)
    reason = "验收合同尚未完整：r2 尚未绑定可用 goal_ids。" + "详细原因" * 200
    task._feedback(task._epoch, "policy_rejected", reason)
    result = task.snapshot()
    assert result["status"] == "review"
    assert result["review_kind"] == "capability"
    assert result["message"].endswith("最近阻碍：" + reason[:500])
    assert reason not in result["message"]
    assert result["feedback"][-1]["message"] == reason


def test_diagnostics_surface_latest_contract_error_alongside_geometry_failure(tmp_path):
    task, _, _ = runner(tmp_path, [act()])
    task.prepare("创建零件")
    value = task.snapshot()
    value["failure_attempts"] = [{"time": "2026-09-06T01:00:00", "call": {"tool": "feature.extrude", "arguments": {}},
                                  "result": {"message": "three circles overlap", "error_code": "InvalidProfile"}}]
    value["feedback"] = [{"time": "2026-09-06T02:00:00", "kind": "policy_rejected", "message": "r2 尚未绑定 goal_ids"}]
    data = inspect_recent_failures(value, task.registry)
    assert any(item["category_hypothesis"] == "policy_or_contract" for item in data["failures"])
    assert any(item["tool"] == "feature.extrude" for item in data["failures"])
    assert "r2" in data["failures"][-1]["message"]
    value["feedback"].extend({"kind": "policy_rejected", "message": f"引用仍错误 {index}"} for index in range(20))
    data = inspect_recent_failures(value, task.registry, limit=2)
    assert len(data["failures"]) == 2
    assert data["failures"][0]["source"] == "cad_failure"
    assert data["failures"][1]["message"] == "引用仍错误 19"


def test_successful_but_ineffective_recovery_steps_do_not_reset_strategy_budget(tmp_path):
    repeated = act([call("session.ensure")], goals=[])
    task, _, _ = runner(tmp_path, [act([call("session.ensure"), call("document.new_part")]), *([repeated] * 20)])
    task.prepare("创建主体")
    value = task.run()
    assert value["status"] == "review"
    assert value["rounds"] <= 8
    assert value["no_progress"] == 6


def test_first_unobserved_named_goal_is_pending_until_actual_readback(tmp_path):
    dimension = goal(id="depth", metric="dimension_mm", target="D1@guessed", expected=10)
    task, worker, _ = runner(tmp_path, [act(goals=[goal(), dimension]), complete()])
    assert task.prepare("创建深十毫米主体")["goals"][1]["binding"] == "pending"
    worker.dimensions = [{"name": "D1@guessed", "value_mm": 10}]
    value = task.run()
    assert value["status"] == "completed"
    assert value["goal_bindings"]["depth"]["verified"] is True
    assert value["goals"][1]["expected"] == 10


def test_legacy_wrong_reference_rebind_is_once_audited_and_numerically_identical(tmp_path):
    task, worker, _ = runner(tmp_path, [act()])
    task.prepare("创建深十毫米主体")
    old = task.snapshot()
    original = goal(id="depth", metric="dimension_mm", target="D1@wrong", expected=10)
    old["goals"].append(original.model_dump(mode="json"))
    old["goal_bindings"] = {}
    task.store.save(old)
    restored = ContinuousTaskRunner(task.registry, worker, Backend([]), task.store)
    restored.restore(old["id"])
    bindings = restored.snapshot()["goal_bindings"]
    obs = {"complete": True, "dimensions": [{"name": "D1@actual", "value_mm": 10}, {"name": "D2@other", "value_mm": 10}]}
    updated = original.model_copy(update={"target": "D1@actual"})
    audit = []
    result = refine_goals(old["goals"], [goal(), updated], obs, bindings=bindings, audit=audit)
    assert bindings["depth"]["verified"] is True
    assert bindings["depth"]["rebindings"] == 1
    assert any(item["kind"] == "legacy_reference_rebound" for item in audit)
    with pytest.raises(ValueError, match="引用不得改变"):
        refine_goals(result, [goal(), updated.model_copy(update={"target": "D2@other"})], obs, bindings=bindings)
    with pytest.raises(ValueError, match="不得放宽"):
        refine_goals(result, [goal(), updated.model_copy(update={"tolerance": 5})], obs, bindings=bindings)


def test_observed_historical_reference_never_becomes_legacy_unverified(tmp_path):
    task, worker, _ = runner(tmp_path, [act()])
    task.prepare("创建深十毫米主体")
    old = task.snapshot()
    dimension = goal(id="depth", metric="dimension_mm", target="D1@actual", expected=10)
    old["goals"].append(dimension.model_dump(mode="json"))
    old["events"].append({"name": "observation_completed", "observation": {
        "complete": True, "dimensions": [{"name": "D1@actual", "value_mm": 10}],
    }})
    task.store.save(old)
    restored = ContinuousTaskRunner(task.registry, worker, Backend([]), task.store)
    result = restored.restore(old["id"])
    assert result["goal_bindings"]["depth"]["verified"] is True
    assert result["goal_bindings"]["depth"]["legacy_unverified"] is False


@pytest.mark.parametrize("kind", ["clarification", "capability"])
def test_local_autonomy_attempts_reset_after_verified_geometry_progress(tmp_path, kind):
    request = TaskDecision(action="review", reason="当前局部问题需要考虑替代", review_kind=kind)
    first = act([call("session.ensure"), call("document.new_part")])
    middle = act([call("sketch.begin", plane="front"), call("sketch.circle", radius_mm=5)], goals=[])
    last = act([call("feature.extrude", depth_mm=10)], goals=[])
    task, _, _ = runner(tmp_path, [request, first, request, middle, request, last, complete()])
    task.prepare("创建主体")
    assert task.run()["status"] == "completed"


def _circle_worker(worker):
    original = worker.call
    circles = []
    identity = "synthetic-document-1"
    sketch_ref = "d" * 64

    def geometry():
        return {"document_identity": identity, "sketch_ref": sketch_ref, "sketch_name": "Sketch1",
                "configuration": "Default", "segments": copy.deepcopy(circles), "geometry_complete": True}

    def invoke(c, timeout_seconds=120):
        if c.tool == "sketch.inspect":
            worker.calls.append(c.tool)
            if worker.callback:
                worker.callback(c)
            data = geometry()
            for item in data["segments"]:
                item["editable"] = True
            return StepResult(True, c, "read circles", data={"complete": True, "sketch": data})
        if c.tool == "sketch.set_circle_radius":
            worker.calls.append(c.tool)
            if worker.callback:
                worker.callback(c)
            before = copy.deepcopy(worker.snapshot)
            failure = worker.fail.get(c.tool)
            if failure:
                return failure(c, before)
            old = geometry()
            target = next(item for item in circles if item["entity_ref"] == c.arguments["entity_ref"])
            old_radius = target["radius_mm"]
            target["radius_mm"] = c.arguments["radius_mm"]
            applied = {"tool": c.tool, "geometry_verified": True, "requires_review": False,
                       "unchanged_other_entities": True, "entity_ref": target["entity_ref"], "sketch_ref": sketch_ref,
                       "old_radius_mm": old_radius, "radius_mm": target["radius_mm"],
                       "geometry_before": old, "geometry_after": geometry(), "mutation_attempted": True}
            return StepResult(True, c, "radius verified", data={"applied": applied}, before=before, after=before)
        if c.tool == "feature.extrude" and circles and circles[-1]["radius_mm"] > 3:
            worker.calls.append(c.tool)
            before = copy.deepcopy(worker.snapshot)
            return StepResult(False, c, "three circles overlap: invalid sketch profile", error_code="InvalidProfile",
                              before=before, after=before)
        result = original(c, timeout_seconds)
        if c.tool == "model.observe":
            result.data.update(document_identity=identity if worker.snapshot.document_title else None,
                               active_sketch_geometry=geometry() if worker.snapshot.active_sketch else [],
                               active_sketch_geometry_complete=True)
        elif c.tool == "sketch.circle":
            ref = str(len(circles) + 1) * 64
            segment = {"entity_ref": ref, "sketch_ref": sketch_ref, "type": 1, "entity_id": [len(circles), 0],
                       "persist_reference": ref, "is_circle": True, "center_sketch_m": [0, 0, 0],
                       "radius_mm": c.arguments["radius_mm"], "normal_sketch": [0, 0, 1]}
            circles.append(segment)
            result.data["applied"].update(creation_verified=True, entity_ref=ref, sketch_ref=sketch_ref,
                                           radius_mm=segment["radius_mm"], center_sketch_m=[0, 0, 0])
        return result

    worker.call = invoke
    return circles


def _circle_task(tmp_path, **kwargs):
    first = act([call("session.ensure"), call("document.new_part"), call("sketch.begin", plane="front"),
                 call("sketch.circle", radius_mm=1), call("sketch.circle", radius_mm=2),
                 call("sketch.circle", radius_mm=5), call("feature.extrude", depth_mm=10)])
    repair = act([call("sketch.set_circle_radius", entity_ref="3" * 64, expected_radius_mm=5, radius_mm=3),
                  call("feature.extrude", depth_mm=10)], goals=[])
    task, worker, backend = runner(tmp_path, [first, repair, complete()], **kwargs)
    circles = _circle_worker(worker)
    return task, worker, backend, circles


def test_three_circle_failure_radius_repair_and_same_extrusion_retry_complete(tmp_path):
    task, worker, backend, circles = _circle_task(tmp_path)
    task.prepare("创建三圆轮廓主体")
    value = task.run()
    assert value["status"] == "completed", value["message"]
    assert worker.calls.count("feature.extrude") == 2
    assert worker.calls.count("sketch.set_circle_radius") == 1
    assert [item["radius_mm"] for item in circles] == [1, 2, 3]
    assert len(value["failure_attempts"]) == 1
    assert value["goals"][0]["expected"] == 1000
    assert backend.contexts[1]["failure_attempts"][0]["call"]["tool"] == "feature.extrude"
    assert len(value["created_circles"]) == 3
    assert value["created_circles"][2]["radius_mm"] == 3


@pytest.mark.parametrize("failure_mode", ["legacy_missing_owner", "identity_change", "partial_failure", "stop_during_inspect"])
def test_circle_repair_keeps_ownership_identity_partial_failure_and_stop_boundaries(tmp_path, failure_mode):
    task, worker, backend, _ = _circle_task(tmp_path)
    task.prepare("创建三圆轮廓主体")
    assert task.run(single_stage=True)["status"] == "ready"
    if failure_mode == "legacy_missing_owner":
        task._task["created_circles"] = []
    elif failure_mode == "identity_change":
        task._task["created_circles"][2]["document"][3] = "another-document"
    elif failure_mode == "partial_failure":
        worker.fail["sketch.set_circle_radius"] = lambda c, before: StepResult(
            False, c, "unknown partial write", error_code="CircleEditError", before=before, after=before,
            data={"circle_edit": {"requires_review": True, "mutation_attempted": True}})
    else:
        worker.callback = lambda c: task.stop() if c.tool == "sketch.inspect" else None
    value = task.run()
    assert value["status"] == ("stopped" if failure_mode == "stop_during_inspect" else "review")
    assert worker.calls.count("feature.extrude") == 1
    assert worker.calls.count("sketch.set_circle_radius") == int(failure_mode == "partial_failure")
    assert value["goals"][0]["expected"] == 1000
    if failure_mode == "legacy_missing_owner":
        assert "缺少本任务已验证的创建引用" in value["message"]


def test_incorrect_expected_radius_is_recoverable_without_mutation(tmp_path):
    task, worker, backend, _ = _circle_task(tmp_path)
    task.prepare("创建三圆轮廓主体")
    task.run(single_stage=True)
    wrong = act([call("sketch.set_circle_radius", entity_ref="3" * 64, expected_radius_mm=4, radius_mm=3)], goals=[])
    right = next(backend.decisions)
    backend.decisions = iter([wrong, right, complete()])
    result = task.run()
    assert result["status"] == "completed"
    assert worker.calls.count("sketch.set_circle_radius") == 1
    assert any("expected_radius_mm" in item["message"] for item in result["feedback"])


def test_circle_fresh_read_round_limit_defers_instead_of_claiming_identity_failure(tmp_path):
    task, worker, backend, _ = _circle_task(tmp_path)
    task.prepare("创建三圆轮廓主体")
    task.run(single_stage=True)
    repair = next(backend.decisions)
    backend.decisions = iter([repair, repair, complete()])
    once = []

    def spend_round_budget():
        if not once:
            once.append(True)
            task._task["round_diagnostics"] = 8

    backend.callback = spend_round_budget
    value = task.run()
    assert value["status"] == "completed"
    assert worker.calls.count("sketch.set_circle_radius") == 1
    assert any("本轮调查额度" in item["message"] for item in value["feedback"])
