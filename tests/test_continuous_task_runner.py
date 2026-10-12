from __future__ import annotations

import copy
import threading

import pytest
from pydantic import ValidationError

from sw_assistant.agent.models import ProposedToolCall
from sw_assistant.agent.task_models import GoalCheck, RequirementCheck, TaskDecision
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.domain import ModelSnapshot, StepResult
from sw_assistant.tools import ToolSpec, build_tool_registry


def goal(**changes):
    values = dict(id="volume", description="体积为 1000 立方毫米", metric="volume_mm3",
                  expected=1000.0, tolerance=0.01)
    values.update(changes)
    return GoalCheck(**values)


def call(tool, **arguments):
    return ProposedToolCall(tool=tool, arguments=arguments)


def act(steps=None, goals=None):
    actual_goals = [goal()] if goals is None else goals
    return TaskDecision(action="act", reason="建立本阶段几何", phase="建立主体",
                        goals=actual_goals,
                        requirements=[RequirementCheck(id="r1", goal_ids=[item.id for item in actual_goals] or ["volume"])],
                        steps=steps or [call("session.ensure"), call("document.new_part"),
                                       call("sketch.begin", plane="front"),
                                       call("sketch.circle", radius_mm=5),
                                       call("feature.extrude", depth_mm=10)])


def complete():
    return TaskDecision(action="complete", reason="请求本地验收")


class Backend:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []
        self.closed = False
        self.callback = None

    def decide(self, context):
        self.contexts.append(copy.deepcopy(context))
        if self.callback:
            self.callback()
        decision = next(self.decisions)
        if isinstance(decision, Exception):
            raise decision
        return decision

    def close(self):
        self.closed = True


class Worker:
    def __init__(self):
        self.snapshot = ModelSnapshot()
        self.calls = []
        self.fail = {}
        self.callback = None
        self.interrupted = False
        self.complete = True
        self.dimensions = []
        self.selection = []

    def call(self, tool_call, timeout_seconds=120):
        self.calls.append(tool_call.tool)
        if self.callback:
            self.callback(tool_call)
        before = copy.deepcopy(self.snapshot)
        tool = tool_call.tool
        data = {}
        if tool == "model.observe":
            data = {"snapshot": self.snapshot.to_dict(), "complete": self.complete,
                    "features": [{"name": f"Feature{i}", "type": "Boss", "error_code": 0}
                                 for i in range(self.snapshot.feature_count)],
                    "planes": [{"name": "Front"}], "dimensions": copy.deepcopy(self.dimensions),
                    "selection": copy.deepcopy(self.selection)}
            return StepResult(True, tool_call, "observed", data=data, after=before)
        failure = self.fail.get(tool)
        if failure:
            return failure(tool_call, before)
        if tool == "session.ensure":
            self.snapshot.connected = True
        elif tool == "document.new_part":
            self.snapshot = ModelSnapshot(connected=True, document_title="Part1", document_type="part", feature_count=3)
        elif tool == "selection.plane":
            self.snapshot.selection_count = 1
        elif tool.startswith("sketch.begin"):
            self.snapshot.active_sketch = "Sketch1"
        elif tool == "sketch.circle":
            self.snapshot.sketch_entity_count += 1
            data = {"applied": {"tool": tool, "frame": {"origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0]},
                                "center_m": [0, 0, 0], "radius_m": tool_call.arguments["radius_mm"] / 1000}}
        elif tool == "feature.extrude":
            self.snapshot.active_sketch = ""
            self.snapshot.sketch_entity_count = 0
            self.snapshot.feature_count += 1
            self.snapshot.body_count = 1
            self.snapshot.volume_m3 += 0.000001
            data = {"applied": {"tool": tool, "depth_m": tool_call.arguments["depth_mm"] / 1000,
                                "operation": tool_call.arguments["operation"], "end_condition": tool_call.arguments["end_condition"],
                                "reverse": tool_call.arguments["reverse"], "merge": tool_call.arguments["merge"]}}
        return StepResult(True, tool_call, "done", data=data, before=before, after=copy.deepcopy(self.snapshot))

    def interrupt(self):
        self.interrupted = True

    def close(self):
        self.interrupted = True


def runner(tmp_path, decisions, **kwargs):
    registry = build_tool_registry()
    if not any(item["name"] == "model.observe" for item in registry.describe()):
        registry.register(ToolSpec("model.observe", "Observe"))
    worker, backend = Worker(), Backend(decisions)
    value = ContinuousTaskRunner(registry, worker, backend, TaskStore(tmp_path / "tasks"), **kwargs)
    return value, worker, backend


def test_prepare_is_cad_free_and_every_successful_stage_feeds_agent(tmp_path):
    first = act([call("session.ensure"), call("document.new_part")])
    second = act([call("sketch.begin", plane="front"), call("sketch.circle", radius_mm=5),
                  call("feature.extrude", depth_mm=10)], goals=[])
    task, worker, backend = runner(tmp_path, [first, second, complete()])
    assert task.prepare("创建体积 1000 的零件")["status"] == "ready"
    assert worker.calls == []
    result = task.run()
    assert result["status"] == "completed"
    assert result["verification"]["passed"]
    assert len(backend.contexts) == 3
    assert len(backend.contexts[1]["completed_steps"]) == 2
    assert len(backend.contexts[2]["completed_evidence"]) == 5
    assert backend.contexts[2]["latest_observation"]["snapshot"]["body_count"] == 1


def test_single_stage_returns_ready_and_next_run_replans(tmp_path):
    task, worker, backend = runner(tmp_path, [act(), complete()])
    task.prepare("part")
    assert task.run(single_stage=True)["status"] == "ready"
    assert len(backend.contexts) == 1
    assert task.run()["status"] == "completed"


def test_invalid_plan_and_malformed_output_consume_rounds_and_can_correct(tmp_path):
    task, worker, backend = runner(tmp_path, [ValueError("invalid JSON"), act([call("sketch.circle", radius_mm=5)]), act(), complete()])
    prepared = task.prepare("part")
    assert prepared["rounds"] == 3
    assert set(worker.calls) <= {"session.capabilities"}
    assert task.run()["status"] == "completed"
    assert any(item["kind"] == "policy_rejected" for item in backend.contexts[2]["feedback"])


def test_invalid_decisions_are_bounded(tmp_path):
    task, worker, backend = runner(tmp_path, [ValueError("bad")] * 3, max_rounds=3)
    result = task.prepare("part")
    assert result["status"] == "review"
    assert result["rounds"] == 3
    assert worker.calls == []


def test_final_false_completion_is_fed_back_and_cannot_pass(tmp_path):
    wrong_goal = goal(expected=9000.0)
    task, worker, backend = runner(tmp_path, [act(goals=[wrong_goal]), complete(), complete()], max_rounds=3)
    task.prepare("part")
    result = task.run()
    assert result["status"] == "review"
    assert not result["verification"]["passed"]
    assert backend.contexts[-1]["verification"]["goals"][0]["actual"] == pytest.approx(1000)


def test_agent_cannot_weaken_locked_goals(tmp_path):
    lowered = TaskDecision(action="complete", reason="done", goals=[goal(expected=0, operator="ge")])
    task, worker, backend = runner(tmp_path, [act(), lowered, complete()])
    task.prepare("part")
    result = task.run()
    assert result["status"] == "completed"
    assert result["goals"][0]["expected"] == 1000
    assert any(item["kind"] == "policy_rejected" for item in backend.contexts[-1]["feedback"])


def test_unobservable_dimension_requires_review(tmp_path):
    goals = [goal(), goal(id="length", metric="dimension_mm", target="D1@Sketch1", expected=10)]
    task, worker, backend = runner(tmp_path, [act(goals=goals), complete()])
    task.prepare("part")
    assert task.run()["status"] == "review"
    assert not task.snapshot()["verification"]["goals"][1]["observable"]


def test_native_postconditions_cannot_be_bypassed_by_success_flag(tmp_path):
    task, worker, backend = runner(tmp_path, [act(), complete()])
    worker.fail["sketch.circle"] = lambda c, b: StepResult(True, c, "success", before=b, after=b)
    task.prepare("part")
    result = task.run()
    assert result["status"] == "review"
    assert "feature.extrude" not in worker.calls
    assert result["failure_attempts"][0]["result"]["error_code"] == "PostconditionFailed"


def test_same_failed_call_blocked_only_in_same_relevant_state(tmp_path):
    step = act([call("selection.plane", plane="front")], goals=[])
    task, worker, backend = runner(tmp_path, [act([call("session.ensure"), call("document.new_part")]), step, step,
                                               TaskDecision(action="review", reason="人工检查")])
    worker.fail["selection.plane"] = lambda c, b: StepResult(False, c, "not found", before=b, after=b)
    task.prepare("part")
    result = task.run()
    assert worker.calls.count("selection.plane") == 1
    assert any(item["kind"] == "repeat_blocked" for item in result["feedback"])


def test_changed_selection_context_allows_same_call_after_manual_review(tmp_path):
    step = act([call("selection.plane", plane="front")], goals=[])
    task, worker, backend = runner(tmp_path, [act([call("session.ensure"), call("document.new_part")]), step,
        TaskDecision(action="review", reason="请选择一个面"), step, TaskDecision(action="review", reason="done")])
    worker.fail["selection.plane"] = lambda c, b: StepResult(False, c, "not found", before=b, after=b)
    task.prepare("part")
    assert task.run(single_stage=True)["status"] == "ready"
    worker.snapshot.selection_count = 1
    worker.selection = [{"type": 2, "persist_reference": "first-face"}]
    assert task.run()["status"] == "review"
    # Changing the identity matters even when both selections have the same count.
    worker.selection = [{"type": 2, "persist_reference": "other-face"}]
    worker.fail.clear()
    assert task.resume("已选择一个有效平面")["status"] == "ready"
    assert task.run()["status"] == "review"
    assert worker.calls.count("selection.plane") == 2


def test_partial_failure_requires_undo_and_fresh_observation(tmp_path):
    task, worker, backend = runner(tmp_path, [act(), TaskDecision(action="review", reason="check")])
    saved = []

    def partial(c, before):
        saved.append(copy.deepcopy(before))
        worker.snapshot.sketch_entity_count += 1
        return StepResult(False, c, "partial", before=before, after=copy.deepcopy(worker.snapshot))

    worker.fail["sketch.circle"] = partial
    task.prepare("part")
    assert task.run()["status"] == "review"
    task.resume("我认为已经好了")
    assert task.run()["status"] == "review"
    assert len(task.snapshot()["completed_steps"]) == 3
    worker.snapshot = saved[0]
    task.resume("撤销了失败步骤")
    task.run()
    assert task.snapshot()["dirty_failure"] is None
    assert len(task.snapshot()["completed_steps"]) == 3


def test_unexpected_document_switch_prevents_writes(tmp_path):
    task, worker, backend = runner(tmp_path, [act([call("session.ensure"), call("document.new_part")]), act()])
    task.prepare("part")
    task.run(single_stage=True)
    worker.snapshot.document_title = "UserPart"
    assert task.run()["status"] == "review"
    assert "sketch.begin" not in worker.calls


def test_stop_during_backend_discards_late_decision(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    backend.callback = task.stop
    result = task.prepare("part")
    assert result["status"] == "stopped"
    assert backend.closed
    assert result["pending_steps"] == []
    assert worker.calls == []


def test_stop_before_prepare_cannot_restart_task(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    task.stop()
    assert task.prepare("part")["status"] == "stopped"
    assert not backend.contexts
    assert not worker.calls


def test_stop_during_worker_ignores_late_success_and_starts_no_more_calls(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    task.prepare("part")
    worker.callback = lambda c: task.stop() if c.tool == "document.new_part" else None
    result = task.run()
    assert result["status"] == "stopped"
    assert worker.interrupted
    assert "sketch.begin" not in worker.calls
    assert [step["tool"] for step in result["completed_steps"]] == ["session.ensure"]
    assert worker.calls[-1] == "document.new_part"


def test_stop_cancels_blocking_backend_without_lock_deadlock(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    entered, release = threading.Event(), threading.Event()
    backend.callback = lambda: (entered.set(), release.wait(2))
    backend.close = release.set
    thread = threading.Thread(target=lambda: task.prepare("part"))
    thread.start()
    assert entered.wait(2)
    task.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert task.snapshot()["status"] == "stopped"


def test_restore_requires_manual_resume_and_fresh_read(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    prepared = task.prepare("part")
    task.run(single_stage=True)
    restored = ContinuousTaskRunner(task.registry, worker, Backend([complete()]), task.store)
    assert restored.restore(prepared["id"])["status"] == "review"
    calls_before = len(worker.calls)
    assert restored.run()["status"] == "review"
    assert len(worker.calls) == calls_before
    restored.resume("已确认仍为此零件")
    assert restored.run()["status"] == "completed"
    assert len(worker.calls) > calls_before


def test_preflight_rejection_never_calls_mutating_tool(tmp_path):
    task, worker, backend = runner(tmp_path, [act()], preflight=lambda c: (False, "busy"))
    task.prepare("part")
    assert task.run()["status"] == "review"
    assert set(worker.calls) == {"model.observe"}


@pytest.mark.parametrize("changes", [dict(expected=float("nan")), dict(tolerance=float("inf")),
                                    dict(metric="dimension_mm", target=""),
                                    dict(metric="body_count", expected=True)])
def test_unverifiable_goal_contract_rejects_invalid_values(changes):
    with pytest.raises(ValidationError):
        goal(**changes)


def test_store_is_atomic_redacts_credentials_and_rejects_path_escape(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    store.save({"id": "safe", "prompt": "part", "api_key": "topsecret",
                "feedback": "password=secretvalue", "status": "ready"})
    data = store.load("safe")
    assert data["api_key"] == "<redacted>"
    assert "secretvalue" not in data["feedback"]
    assert store.recent()[0]["id"] == "safe"
    assert store.load("missing") is None
    assert not list(store.path.glob("*.tmp"))
    with pytest.raises(ValueError):
        store.load("../outside")


def test_backend_unavailability_pauses_without_48_repeated_requests(tmp_path):
    class Unavailable(RuntimeError):
        fatal = True

    task, worker, backend = runner(tmp_path, [Unavailable("missing model settings")])
    assert task.prepare("part")["status"] == "review"
    assert len(backend.contexts) == 1
    assert worker.calls == []


def test_unexpected_backend_runtime_error_is_not_a_repairable_decision(tmp_path):
    task, worker, backend = runner(tmp_path, [RuntimeError("network broken")])
    assert task.prepare("part")["status"] == "review"
    assert len(backend.contexts) == 1


def test_no_goal_means_no_mutation(tmp_path):
    task, worker, backend = runner(tmp_path, [act(goals=[])] * 2, max_rounds=2)
    assert task.prepare("part")["status"] == "review"
    assert set(worker.calls) <= {"session.capabilities"}


def test_observation_must_be_complete_before_first_action(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    task.prepare("part")
    worker.complete = False
    assert task.run()["status"] == "review"
    assert set(worker.calls) <= {"model.observe", "session.capabilities"}


def test_existing_user_model_cannot_be_claimed_as_new_task_completion(tmp_path):
    decision = TaskDecision(action="complete", reason="already done", goals=[goal()],
                            requirements=[RequirementCheck(id="r1", goal_ids=["volume"])])
    task, worker, backend = runner(tmp_path, [decision], max_rounds=1)
    worker.snapshot = ModelSnapshot(connected=True, document_title="Existing", document_type="part", body_count=1, volume_m3=0.000001)
    task.prepare("build a new part")
    result = task.run()
    assert result["status"] == "review"
    assert not result["verification"]["passed"]
    assert not next(check for check in result["verification"]["implicit"] if check["id"] == "task_created_document")["passed"]


def test_unknown_failure_state_needs_manual_reconciliation(tmp_path):
    task, worker, backend = runner(tmp_path, [act()])
    worker.fail["document.new_part"] = lambda c, b: StepResult(False, c, "worker lost")
    task.prepare("part")
    result = task.run()
    assert result["status"] == "review"
    assert result["dirty_failure"]
    assert "sketch.begin" not in worker.calls


def test_restart_rebind_requires_same_observed_geometry(tmp_path):
    task, worker, backend = runner(tmp_path, [act()], task_id="fixed-session")
    assert task.prepare("part")["id"] == "fixed-session"
    task.run(single_stage=True)
    saved = task.snapshot()
    saved["expected_observation"]["document_identity"] = "old-process"
    task.store.save(saved)
    restored = ContinuousTaskRunner(task.registry, worker, Backend([complete()]), task.store)
    restored.restore("fixed-session")
    restored.resume("重新打开程序后确认是同一零件")
    assert restored.run()["status"] == "completed"


def test_numerical_mass_property_noise_is_not_an_external_model_edit(tmp_path):
    task, worker, backend = runner(tmp_path, [act(), complete()])
    original = worker.call

    def noisy(c, timeout_seconds=120):
        result = original(c, timeout_seconds)
        if c.tool == "feature.extrude":
            result.after.volume_m3 += 1e-14
        return result

    worker.call = noisy
    task.prepare("part")
    assert task.run()["status"] == "completed"


def test_closing_completed_runner_keeps_completion_record_unchanged(tmp_path):
    task, worker, backend = runner(tmp_path, [act(), complete()])
    task.prepare("part")
    result = task.run()
    assert result["status"] == "completed"
    saved_before = task.store.load(result["id"])
    task.stop()
    assert backend.closed
    assert worker.interrupted
    assert task.snapshot() == result
    assert task.store.load(result["id"]) == saved_before


def test_stop_while_restore_reads_cannot_overwrite_stopped_state(tmp_path, monkeypatch):
    source, worker, backend = runner(tmp_path, [act()])
    saved = source.prepare("part")
    restoring = ContinuousTaskRunner(source.registry, worker, Backend([]), source.store,
                                    task_id=saved["id"])
    entered, release = threading.Event(), threading.Event()
    original_load = source.store.load
    returned = []

    def slow_load(task_id):
        record = original_load(task_id)
        entered.set()
        assert release.wait(3)
        return record

    monkeypatch.setattr(source.store, "load", slow_load)
    thread = threading.Thread(target=lambda: returned.append(restoring.restore(saved["id"])))
    thread.start()
    assert entered.wait(3)
    restoring.stop()
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert returned[0]["status"] == "stopped"
    assert restoring.snapshot()["status"] == "stopped"
    assert original_load(saved["id"]) == saved
    assert worker.calls == []
    with pytest.raises(ValueError, match="待人工检查"):
        restoring.resume("继续")


def test_stop_before_restore_does_not_read_or_rewrite_saved_record(tmp_path, monkeypatch):
    source, worker, backend = runner(tmp_path, [act()])
    saved = source.prepare("part")
    restored = ContinuousTaskRunner(source.registry, worker, Backend([]), source.store,
                                   task_id=saved["id"])
    restored.stop()
    reads = []
    monkeypatch.setattr(source.store, "load", lambda task_id: reads.append(task_id))
    assert restored.restore(saved["id"])["status"] == "stopped"
    assert reads == []


class ReattachedWorker(Worker):
    """A replaced COM helper: CAD remains open but the helper has no connection."""

    def __init__(self, snapshot):
        super().__init__()
        self.snapshot = copy.deepcopy(snapshot)
        self.attached = False

    def call(self, tool_call, timeout_seconds=120):
        if tool_call.tool == "model.observe" and not self.attached:
            self.calls.append(tool_call.tool)
            return StepResult(True, tool_call, "disconnected", data={
                "snapshot": ModelSnapshot().to_dict(), "complete": True,
                "features": [], "planes": [], "dimensions": [], "selection": [],
            })
        result = super().call(tool_call, timeout_seconds)
        if tool_call.tool == "session.ensure":
            self.attached = True
        if tool_call.tool == "model.observe":
            result.data["document_identity"] = "replacement-helper-document"
        return result


def restored_detached_task(tmp_path, *, first=None, later=None):
    source, initial_worker, initial_backend = runner(tmp_path, [first or act()])
    saved = source.prepare("part")
    source.run(single_stage=True)
    source.stop()
    worker = ReattachedWorker(initial_worker.snapshot)
    backend = Backend(later or [complete()])
    restored = ContinuousTaskRunner(source.registry, worker, backend, source.store,
                                   task_id=saved["id"])
    restored.restore(saved["id"])
    restored.resume("确认原零件仍在 SolidWorks 中打开，请重新连接")
    return restored, worker, backend


def test_replaced_helper_reconnects_before_verification_without_replaying_geometry(tmp_path):
    task, worker, backend = restored_detached_task(tmp_path)
    result = task.run()
    assert result["status"] == "completed"
    assert worker.calls[:3] == ["model.observe", "session.ensure", "model.observe"]
    assert worker.calls.count("session.ensure") == 1
    assert "document.new_part" not in worker.calls
    assert sum(step["tool"] == "session.ensure" for step in result["completed_steps"]) == 1
    assert any(item["kind"] == "connection_refreshed" for item in result["feedback"])


def test_reconnected_complete_feature_checkpoint_can_continue_on_stable_plane(tmp_path):
    next_stage = act([call("sketch.begin", plane="top"), call("sketch.circle", radius_mm=5),
                      call("feature.extrude", depth_mm=10)], goals=[])
    task, worker, backend = restored_detached_task(
        tmp_path, first=act(goals=[goal(expected=2000)]), later=[next_stage, complete()],
    )
    result = task.run()
    assert result["status"] == "completed"
    assert worker.calls.count("feature.extrude") == 1
    assert "document.new_part" not in worker.calls
    assert result["gateway_context_reset"] is False


def test_reconnect_to_wrong_document_still_requires_review(tmp_path):
    task, worker, backend = restored_detached_task(tmp_path)
    worker.snapshot.document_title = "DifferentPart"
    result = task.run()
    assert result["status"] == "review"
    assert worker.calls == ["model.observe", "session.ensure", "model.observe"]
    assert backend.contexts == []


def test_active_sketch_binding_is_not_fabricated_after_helper_restart(tmp_path):
    first = act([call("session.ensure"), call("document.new_part"), call("sketch.begin", plane="top")])
    task, worker, backend = restored_detached_task(tmp_path, first=first)
    result = task.run()
    assert result["status"] == "review"
    assert "活动草图" in result["message"]
    assert backend.contexts == []
    assert "sketch.circle" not in worker.calls


def test_stop_during_connection_refresh_does_not_accept_late_reconnect(tmp_path):
    task, worker, backend = restored_detached_task(tmp_path)
    worker.callback = lambda c: task.stop() if c.tool == "session.ensure" else None
    result = task.run()
    assert result["status"] == "stopped"
    assert worker.calls == ["model.observe", "session.ensure"]
    assert backend.contexts == []
