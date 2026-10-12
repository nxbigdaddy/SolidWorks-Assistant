from __future__ import annotations

import copy

import pytest

from sw_assistant.agent.task_models import RequirementCheck, TaskDecision
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.domain import StepResult

from test_continuous_task_runner import Backend, act, call, complete, goal, runner


def requirement(identifier="r1", *goals):
    return RequirementCheck(id=identifier, goal_ids=list(goals) or ["volume"])


def inspect(*steps):
    return TaskDecision(action="inspect", reason="查询具体的操作证据", steps=list(steps))


def test_requested_inspection_tools_execute_and_feed_structured_results(tmp_path):
    investigation = inspect(call("session.capabilities"), call("model.references", limit=5),
                            call("sketch.inspect"), call("feature.inspect", name="Boss"),
                            call("diagnostics.recent", limit=2))
    task, worker, backend = runner(tmp_path, [investigation, act(), complete()])
    task.prepare("建立零件")
    assert worker.calls == []
    result = task.run()
    assert result["status"] == "completed"
    history = backend.contexts[1]["diagnostic_history"]
    assert {item["tool"] for item in history} == {step.tool for step in investigation.steps}
    assert next(item for item in history if item["tool"] == "model.references")["arguments"]["limit"] == 5
    assert "diagnostics.recent" not in worker.calls
    assert "feature.inspect" in worker.calls


def test_native_investigation_bridge_is_read_only_and_bounded_per_round(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    original = worker.call
    results = []

    def feature_reader(c, timeout_seconds=120):
        if c.tool == "feature.inspect":
            worker.calls.append(c.tool)
            return StepResult(True, c, "read", data={"complete": True, "name": c.arguments["name"]})
        return original(c, timeout_seconds)

    worker.call = feature_reader

    def investigate_during_model():
        results.append(task.investigate("document.new_part", {}))
        results.extend(task.investigate("feature.inspect", {"name": f"F{index}"}) for index in range(9))

    backend.callback = investigate_during_model
    assert task.prepare("建立零件")["status"] == "ready"
    assert not results[0]["ok"]
    assert sum(item["ok"] for item in results) == 7
    assert results[-1]["error_code"] == "RoundDiagnosticLimit"
    assert set(worker.calls) == {"feature.inspect"}


@pytest.mark.parametrize("kind", ["clarification", "capability"])
def test_noncritical_review_first_tries_an_autonomous_route(tmp_path, kind):
    request = TaskDecision(action="review", review_kind=kind, reason="缺少一个可选择的默认条件",
                           questions=["是否使用常见默认尺寸？"])
    plan = act()
    plan.assumptions = ["未指定的位置采用中心对齐，可在后续调整。"]
    task, worker, backend = runner(tmp_path, [request, plan, complete()])
    assert task.prepare("建立支架")["status"] == "ready"
    assert task.run()["status"] == "completed"
    assert task.snapshot()["assumptions"] == plan.assumptions
    assert any(item["kind"] == "autonomy_retry" for item in backend.contexts[1]["feedback"])
    if kind == "capability":
        assert "session.capabilities" in worker.calls


def test_safety_and_material_user_choices_still_pause_immediately(tmp_path):
    request = TaskDecision(action="review", review_kind="clarification", review_critical=True,
                           reason="承载工况决定尺寸，不能假设", questions=["最大载荷是多少？"])
    task, worker, backend = runner(tmp_path, [request])
    assert task.prepare("设计承重支架")["status"] == "review"
    assert worker.calls == []
    assert len(backend.contexts) == 1


def test_untyped_preflight_denial_cannot_authorize_automatic_recovery_from_text(tmp_path):
    refused = []

    def preflight(c):
        if c.tool == "sketch.begin" and not refused:
            refused.append(True)
            return False, "基准面暂不可选"
        return True, ""

    remaining = act([call("sketch.begin", plane="top"), call("sketch.circle", radius_mm=5),
                     call("feature.extrude", depth_mm=10)], goals=[])
    task, worker, backend = runner(tmp_path, [act(), remaining, complete()], preflight=preflight)
    task.prepare("创建轴类零件")
    result = task.run()
    assert result["status"] == "review"
    assert worker.calls.count("document.new_part") == 1
    assert "sketch.begin" not in worker.calls
    assert result["last_failure"]["failure_category"] == "safety"
    assert len(backend.contexts) == 1


def test_known_document_incomplete_observation_can_be_investigated_before_writes(tmp_path):
    task, worker, backend = runner(tmp_path, [act(), inspect(call("sketch.inspect")), act(), complete()])
    task.prepare("创建基座")
    worker.complete = False
    worker.callback = lambda c: setattr(worker, "complete", True) if c.tool == "sketch.inspect" else None
    result = task.run()
    assert result["status"] == "completed"
    assert worker.calls.index("sketch.inspect") < worker.calls.index("document.new_part")
    assert any(item["kind"] == "observation_incomplete" for item in result["feedback"])


def test_missing_requirement_mapping_rejected_then_completed_with_measured_goal(tmp_path):
    dimension = goal(id="length", metric="dimension_mm", target="D1", expected=10)
    incomplete = act(goals=[goal(), dimension])
    corrected = incomplete.model_copy(deep=True)
    corrected.requirements = [requirement("r1", "volume"), requirement("r2", "length")]
    task, worker, backend = runner(tmp_path, [incomplete, corrected, complete()])
    worker.dimensions = [{"name": "D1", "value_mm": 10}]
    task.prepare("创建基座。长度必须是十毫米。")
    assert task.run()["status"] == "completed"
    assert len(backend.contexts[1]["requirements"]) == 2
    assert any("r2" in item["message"] for item in backend.contexts[1]["feedback"])


def test_body_or_document_count_alone_is_not_a_geometric_acceptance_contract(tmp_path):
    body = goal(id="body", metric="body_count", expected=1)
    task, worker, backend = runner(tmp_path, [act(goals=[body])] * 3, max_rounds=3)
    assert task.prepare("建立有定位结构的连接板")["status"] == "review"
    assert "document.new_part" not in worker.calls
    assert "session.ensure" not in worker.calls


def test_refinement_can_narrow_but_not_weaken_a_locked_numeric_goal(tmp_path):
    bound = goal(operator="ge", expected=500)
    refined = TaskDecision(action="complete", reason="已读回目标体积，收紧验收范围", goals=[goal(expected=1000)])
    task, worker, backend = runner(tmp_path, [act(goals=[bound]), refined])
    task.prepare("创建小于指定体积的零件")
    result = task.run()
    assert result["status"] == "completed"
    assert result["goals"][0]["operator"] == "eq"


def test_pending_dimension_reference_must_bind_to_an_observed_name(tmp_path):
    dimension = goal(id="depth", metric="dimension_mm", expected=10, target="", binding="pending")
    bound = dimension.model_copy(update={"binding": "fixed", "target": "D1@Boss"})
    finished = TaskDecision(action="complete", reason="已取得实际尺寸名称", goals=[goal(), bound])
    task, worker, backend = runner(tmp_path, [act(goals=[goal(), dimension]), finished])
    worker.callback = lambda c: setattr(worker, "dimensions", [{"name": "D1@Boss", "value_mm": 10}]) if c.tool == "feature.extrude" else None
    task.prepare("建立深十毫米的主体")
    assert task.run()["status"] == "completed"
    assert task.snapshot()["goals"][1]["binding"] == "fixed"


def test_legacy_record_requires_acceptance_mapping_before_completion(tmp_path):
    source, worker, backend = runner(tmp_path, [act()])
    prepared = source.prepare("原始零件要求")
    source.run(single_stage=True)
    legacy = source.snapshot()
    legacy.pop("requirements")
    source.store.save(legacy)
    corrected = complete()
    corrected.requirements = [requirement()]
    restored_backend = Backend([complete(), corrected])
    restored = ContinuousTaskRunner(source.registry, worker, restored_backend, source.store)
    restored.restore(prepared["id"])
    restored.resume("检查了原模型")
    assert restored.run()["status"] == "completed"
    assert any(item["kind"] == "policy_rejected" for item in restored_backend.contexts[1]["feedback"])


def test_steering_during_model_discards_old_decision_and_preserves_source_messages(tmp_path):
    extra = goal(id="length", metric="dimension_mm", target="D1", expected=10)
    revised = act(goals=[goal(), extra])
    revised.requirements = [requirement(), requirement("r2", "length")]
    task, worker, backend = runner(tmp_path, [act(), revised, complete()])
    sent = []

    def steer_once():
        if not sent:
            sent.append(True)
            task.steer("再增加十毫米长度要求")

    backend.callback = steer_once
    worker.dimensions = [{"name": "D1", "value_mm": 10}]
    prepared = task.prepare("创建基座")
    assert prepared["rounds"] == 2
    assert worker.calls == []
    assert backend.contexts[1]["messages"][1]["id"] == "u2"
    assert len(prepared["requirements"]) == 2
    assert task.run()["status"] == "completed"


def test_completed_task_can_extend_at_a_boundary_without_replaying_successes(tmp_path):
    original_goal = goal(operator="ge", expected=1000)
    added = goal(id="second", metric="feature_present", target="Feature4", expected=True)
    extension = act([call("sketch.begin", plane="top"), call("sketch.circle", radius_mm=5),
                     call("feature.extrude", depth_mm=10)], goals=[original_goal, added])
    extension.requirements = [requirement(), requirement("r2", "second")]
    task, worker, backend = runner(tmp_path, [act(goals=[original_goal]), complete(), extension, complete()])
    task.prepare("创建至少指定体积的基座")
    assert task.run()["status"] == "completed"
    original_prefix = copy.deepcopy(task.snapshot()["completed_steps"])
    assert task.steer("再增加一个凸台特征")["status"] == "ready"
    result = task.run()
    assert result["status"] == "completed"
    assert result["completed_steps"][:len(original_prefix)] == original_prefix
    assert worker.calls.count("document.new_part") == 1
    assert len(result["verification_history"]) == 1
    assert result["requirements"][1]["source_message_id"] == "u2"


def test_steering_does_not_revive_safety_review_or_stopped_task(tmp_path):
    request = TaskDecision(action="review", review_kind="safety", reason="需要确认文档所有权")
    task, worker, backend = runner(tmp_path, [request])
    task.prepare("创建零件")
    assert task.steer("继续增加一个孔")["status"] == "review"
    assert task.run()["status"] == "review"
    task.stop()
    with pytest.raises(ValueError):
        task.steer("继续")
    assert worker.calls == []


def test_repeated_unchanged_investigation_pauses_before_all_48_rounds(tmp_path):
    task, worker, backend = runner(tmp_path, [inspect(call("session.capabilities"))] * 20)
    task.prepare("调查可用建模方式")
    result = task.run()
    assert result["status"] == "review"
    assert result["rounds"] <= 8
    assert "进展" in result["message"]


def test_stop_during_native_investigation_discards_its_late_result(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    returned = []
    backend.callback = lambda: returned.append(task.investigate("session.capabilities", {}))
    worker.callback = lambda c: task.stop() if c.tool == "session.capabilities" else None
    result = task.prepare("创建零件")
    assert result["status"] == "stopped"
    assert returned[0]["error_code"] == "TaskStopped"
    assert result["diagnostic_history"] == []
    assert worker.calls == ["session.capabilities"]


def test_user_steering_during_cad_waits_for_result_then_replans_remaining_steps(tmp_path):
    original_goal = goal(operator="ge", expected=1000)
    added = goal(id="boss", metric="feature_present", target="Feature3", expected=True)
    revised = act([call("sketch.begin", plane="top"), call("sketch.circle", radius_mm=5),
                   call("feature.extrude", depth_mm=10)], goals=[original_goal, added])
    revised.requirements = [requirement(), requirement("r2", "boss")]
    task, worker, backend = runner(tmp_path, [act(goals=[original_goal]), revised, complete()])
    task.prepare("建立满足体积下限的基座")
    worker.callback = lambda c: task.steer("再确认包含一个凸台特征") if c.tool == "document.new_part" else None
    result = task.run()
    assert result["status"] == "completed"
    assert len(backend.contexts[1]["completed_steps"]) == 2
    assert worker.calls.count("document.new_part") == 1
    assert result["completed_steps"][2]["arguments"]["plane"] == "top"


def test_steering_during_final_observation_prevents_stale_completion(tmp_path):
    original_goal = goal(operator="ge", expected=1000)
    added = goal(id="second", metric="feature_present", target="Feature4", expected=True)
    extension = act([call("sketch.begin", plane="top"), call("sketch.circle", radius_mm=5),
                     call("feature.extrude", depth_mm=10)], goals=[original_goal, added])
    extension.requirements = [requirement(), requirement("r2", "second")]
    task, worker, backend = runner(tmp_path, [act(goals=[original_goal]), complete(), extension, complete()])
    sent = []

    def on_call(c):
        if c.tool == "model.observe" and len(backend.contexts) == 2 and not sent:
            sent.append(True)
            task.steer("再增加第二个凸台")

    worker.callback = on_call
    task.prepare("创建满足体积下限的基座")
    result = task.run()
    assert result["status"] == "completed"
    assert len(result["requirements"]) == 2
    assert len(result["goals"]) == 2
    assert sum(event["name"] == "task_completed" for event in result["events"]) == 1
    assert worker.calls.count("feature.extrude") == 2


@pytest.mark.parametrize("operator,tolerance", [("ge", 1_000_000_000), ("eq", 2), ("le", 0.001)])
def test_tautological_or_only_upper_bound_is_not_concrete_geometry(tmp_path, operator, tolerance):
    weak = goal(expected=1, operator=operator, tolerance=tolerance)
    task, worker, backend = runner(tmp_path, [act(goals=[weak])] * 2, max_rounds=2)
    result = task.prepare("创建带结构细节的连接件")
    assert result["status"] == "review"
    assert "document.new_part" not in worker.calls


@pytest.mark.parametrize("feature_type", ["RefPlane", "OriginProfileFeature", "ProfileFeature", None])
def test_template_or_unknown_feature_presence_is_not_task_geometry_evidence(tmp_path, feature_type):
    feature = goal(metric="feature_present", target="TemplateFeature", expected=True)
    task, worker, backend = runner(tmp_path, [act(goals=[feature]), complete(), complete()], max_rounds=3)
    original = worker.call

    def with_template(c, timeout_seconds=120):
        result = original(c, timeout_seconds)
        if c.tool == "model.observe" and worker.snapshot.document_title:
            result.data["features"].append({"name": "TemplateFeature", "type": feature_type, "error_code": 0})
        return result

    worker.call = with_template
    task.prepare("创建带完整建模细节的零件")
    result = task.run()
    assert result["status"] == "review"
    assert not result["verification"]["passed"]
    assert not result["verification"]["goals"][0]["geometry_evidence"]


def test_resume_notes_remain_in_messages_and_contract_after_recent_feedback_window(tmp_path):
    question = TaskDecision(action="review", review_kind="user_decision", reason="是否确认采用当前中心对齐方案？")
    task, worker, backend = runner(tmp_path, [act(), question, complete()])
    task.prepare("建立中心对齐的基座")
    assert task.run()["status"] == "review"
    note = "同意现有验收尺寸和中心对齐方案"
    task.resume(note)
    for index in range(25):
        task._feedback(task._epoch, "progress", f"后续状态记录 {index}")
    assert task.run()["status"] == "completed"
    context = backend.contexts[-1]
    assert all(note != item["message"] for item in context["feedback"])
    assert any(item["content"] == note for item in context["messages"])
    assert any(item["source_text"] == note for item in context["requirements"])
    saved = task.store.load(task.snapshot()["id"])
    assert saved["messages"][-1]["content"] == note


def test_safety_resume_note_does_not_create_an_extra_geometry_requirement(tmp_path):
    review = TaskDecision(action="review", review_kind="safety", reason="请核对当前草图已退出")
    task, worker, backend = runner(tmp_path, [act(), review, complete()])
    task.prepare("建立零件")
    assert task.run()["status"] == "review"
    task.resume("已退出草图，当前文档和已完成特征没有改变")
    assert len(task.snapshot()["requirements"]) == 1
    assert task.snapshot()["messages"][-1]["purpose"] == "review_feedback"
    assert task.run()["status"] == "completed"


def test_no_progress_budget_never_downgrades_a_permission_review(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    task.prepare("创建零件")
    task._task["no_progress"] = 5
    task.preflight = lambda c: (False, "permission denied")
    result = task.investigate("session.capabilities", {})
    assert not result["ok"]
    assert task.snapshot()["status"] == "review"
    assert task.snapshot()["review_kind"] == "safety"
    assert task.snapshot()["message"] == "permission denied"
    assert worker.calls == []


@pytest.mark.parametrize("status", ["review", "completed", "stopped"])
def test_late_feedback_preserves_the_blocking_or_terminal_message(tmp_path, status):
    task, worker, backend = runner(tmp_path, [act()])
    task.prepare("创建零件")
    task._change(task._epoch, status=status, message="应保留的阻塞或结束原因")
    task._feedback(task._epoch, "investigation_feedback", "晚到的普通调查说明")
    result = task.snapshot()
    assert result["status"] == status
    assert result["message"] == "应保留的阻塞或结束原因"
    assert result["feedback"][-1]["message"] == "晚到的普通调查说明"
    task._task["failure_budget"]["counts"]["planning"] = 5
    task._change(task._epoch, no_progress=5)
    task._feedback(task._epoch, "policy_rejected", "晚到的第六次错误不得重新进入 review")
    assert task.snapshot()["no_progress"] == (6 if status == "review" else 5)
    assert task.snapshot()["status"] == status
    assert task.snapshot()["message"] == "应保留的阻塞或结束原因"
