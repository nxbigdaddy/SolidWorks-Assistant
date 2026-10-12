from pathlib import Path

import pytest

from sw_assistant.domain import (
    ExecutionState,
    ModelSnapshot,
    Plan,
    PlanningError,
    ToolCall,
    ToolError,
)
from sw_assistant.execution import ExecutionEngine
from sw_assistant.execution.recovery import assess_failure
from sw_assistant.infrastructure import AuditLog
from sw_assistant.tools import build_tool_registry

from fakes import FakeWorker, result


def _engine(tmp_path: Path, worker: FakeWorker, events: list):
    return ExecutionEngine(
        build_tool_registry(),
        worker,
        AuditLog(tmp_path / "audit.jsonl"),
        lambda name, payload: events.append((name, payload)),
    )


def test_clean_plan_executes_one_step_at_a_time(tmp_path):
    events = []
    worker = FakeWorker(
        [result(before=ModelSnapshot(connected=False), after=ModelSnapshot(connected=True))]
    )
    engine = _engine(tmp_path, worker, events)
    engine.load_plan(Plan("connect", [ToolCall("session.ensure", {})]))
    engine.execute_next()
    assert engine.state == ExecutionState.COMPLETED
    assert engine.cursor == 1
    assert any(name == "plan_completed" for name, _ in events)


def test_api_success_without_postcondition_pauses(tmp_path):
    worker = FakeWorker(
        [result(before=ModelSnapshot(connected=False), after=ModelSnapshot(connected=False))]
    )
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(Plan("connect", [ToolCall("session.ensure", {})]))
    value = engine.execute_next()
    assert value.ok is False
    assert value.error_code == "PostconditionFailed"
    assert engine.state == ExecutionState.PAUSED


def test_preflight_rejection_pauses_before_calling_worker(tmp_path):
    worker = FakeWorker()
    engine = ExecutionEngine(
        build_tool_registry(),
        worker,
        AuditLog(tmp_path / "audit.jsonl"),
        preflight=lambda _call: (False, "SolidWorks 正在执行其他命令。"),
    )
    engine.load_plan(Plan("connect", [ToolCall("session.ensure", {})]))

    value = engine.execute_next()

    assert value is not None and value.error_code == "ContextUnavailable"
    assert engine.state == ExecutionState.PAUSED
    assert worker.calls == []


def test_failure_preserves_prefix_and_can_skip(tmp_path):
    worker = FakeWorker(
        [
            result(before=ModelSnapshot(False), after=ModelSnapshot(True)),
            result(ok=False, message="failed"),
        ]
    )
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(
        Plan("task", [ToolCall("session.ensure", {}), ToolCall("document.new_part", {})])
    )
    engine.execute_next()
    engine.execute_next()
    assert engine.cursor == 1
    assert engine.state == ExecutionState.PAUSED
    engine.skip_failed()
    assert engine.state == ExecutionState.COMPLETED
    assert engine.skipped == {1}


def test_identical_retry_is_bounded(tmp_path):
    failure = result(ok=False, message="same")
    worker = FakeWorker([failure, result(ok=False, message="same")])
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(Plan("task", [ToolCall("session.ensure", {})]))
    engine.execute_next()
    engine.retry_failed()
    with pytest.raises(ToolError, match="已经重试过一次"):
        engine.retry_failed()


def test_retry_is_blocked_when_failed_call_already_changed_topology(tmp_path):
    changed = result(
        ok=False,
        message="postcondition failed",
        before=ModelSnapshot(
            connected=True,
            document_title="Part1",
            document_type="part",
            feature_count=3,
            body_count=1,
            volume_m3=0.0001,
        ),
        after=ModelSnapshot(
            connected=True,
            document_title="Part1",
            document_type="part",
            feature_count=4,
            body_count=1,
            volume_m3=0.0002,
        ),
    )
    engine = _engine(tmp_path, FakeWorker([changed]), [])
    engine.load_plan(Plan("inspect", [ToolCall("model.inspect", {})]))
    engine.execute_next()

    with pytest.raises(ToolError, match="已经改变了.*拓扑"):
        engine.retry_failed()


def test_recovery_classifier_distinguishes_safe_retry_ai_repair_and_dirty_model():
    stable = ModelSnapshot(
        connected=True,
        document_title="Part1",
        document_type="part",
        active_sketch="Sketch1",
        sketch_entity_count=0,
    )
    transient = result(
        ok=False,
        message="RPC_E_CALL_REJECTED: server busy",
        before=stable,
        after=stable,
    )
    transient.error_code = "ToolError"
    transient_assessment = assess_failure(transient)
    assert transient_assessment.category == "transient"
    assert transient_assessment.safe_retry is True
    assert transient_assessment.ai_repair_recommended is False

    geometry = result(
        ok=False,
        message="SolidWorks 创建正多边形失败；已自动回滚本次新增草图实体。",
        before=stable,
        after=stable,
    )
    geometry.error_code = "ToolError"
    geometry_assessment = assess_failure(geometry)
    assert geometry_assessment.category == "geometry"
    assert geometry_assessment.ai_repair_recommended is True
    assert geometry_assessment.manual_review_required is False

    changed = result(
        ok=False,
        message="草图创建失败",
        before=stable,
        after=ModelSnapshot(
            connected=True,
            document_title="Part1",
            document_type="part",
            active_sketch="Sketch1",
            sketch_entity_count=1,
        ),
    )
    changed.error_code = "ToolError"
    changed_assessment = assess_failure(changed)
    assert changed_assessment.model_changed is True
    assert changed_assessment.ai_repair_recommended is False
    assert changed_assessment.manual_review_required is True


def test_replace_failed_validates_arguments(tmp_path):
    worker = FakeWorker(
        [
            result(
                before=ModelSnapshot(connected=True),
                after=ModelSnapshot(
                    connected=True, document_title="Part1", document_type="part"
                ),
            ),
            result(
                before=ModelSnapshot(connected=True, document_type="part"),
                after=ModelSnapshot(
                    connected=True,
                    document_type="part",
                    active_sketch="Sketch1",
                ),
            ),
            result(ok=False, message="bad"),
        ]
    )
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(
        Plan(
            "task",
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.circle", {"radius_mm": 2}),
            ],
        )
    )
    engine.run_all()
    engine.replace_failed(ToolCall("sketch.circle", {"radius_mm": 3}))
    assert engine.plan.steps[2].arguments["radius_mm"] == 3
    assert engine.state == ExecutionState.READY


def test_recovery_context_is_redacted_and_replaces_only_suffix(tmp_path):
    events = []
    worker = FakeWorker(
        [
            result(before=ModelSnapshot(False), after=ModelSnapshot(True)),
            result(
                before=ModelSnapshot(connected=True),
                after=ModelSnapshot(
                    connected=True, document_title="Part1", document_type="part"
                ),
            ),
            result(
                ok=False,
                message="plane missing",
                after=ModelSnapshot(
                    connected=True,
                    document_title="Part1",
                    document_path=r"C:\private\Part1.SLDPRT",
                    document_type="part",
                ),
            ),
        ]
    )
    engine = _engine(tmp_path, worker, events)
    engine.load_plan(
        Plan(
            "task",
            [
                ToolCall("session.ensure", {}),
                ToolCall("document.new_part", {}),
                ToolCall("selection.plane", {"plane": "front"}),
                ToolCall("sketch.begin", {"plane": "front"}),
            ],
        )
    )
    engine.run_all()
    context = engine.recovery_context()
    assert "document_path" not in context["current_model_state"]
    assert [step["tool"] for step in context["completed_steps"]] == [
        "session.ensure",
        "document.new_part",
    ]
    assert context["coordinate_contract"]["right"] == "local(x,y) -> world(Y,Z)"
    assert "sketch.polygon" in context["available_tools"]
    engine.replace_remaining(
        [
            ToolCall("selection.plane", {"plane": "top"}),
            ToolCall("sketch.begin", {"plane": "top"}),
        ]
    )
    assert [step.arguments["plane"] for step in engine.plan.steps[2:]] == ["top", "top"]
    replacement = [payload for name, payload in events if name == "remaining_replaced"][-1]
    assert [call["tool"] for call in replacement["calls"]] == [
        "selection.plane",
        "sketch.begin",
    ]


def test_engine_rejects_semantically_invalid_plan_at_load(tmp_path):
    engine = _engine(tmp_path, FakeWorker(), [])
    with pytest.raises(PlanningError, match="必须先创建草图"):
        engine.load_plan(Plan("invalid", [ToolCall("sketch.circle", {"radius_mm": 2})]))


def test_recovery_context_redacts_paths_from_all_model_bound_fields(tmp_path):
    local_path = r"C:\private folder\Part1.SLDPRT"
    worker = FakeWorker([result(ok=False, message=f"cannot open {local_path}")])
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(
        Plan(
            f"inspect {local_path}",
            [ToolCall("document.open_readonly", {"path": local_path})],
        )
    )
    engine.execute_next()
    context = engine.recovery_context()
    encoded = str(context)
    assert local_path not in encoded
    assert "C:\\" not in encoded
    assert context["failed_step"]["arguments"]["path"] == "<local-path>"


def test_skipped_steps_are_not_reported_as_completed_during_recovery(tmp_path):
    worker = FakeWorker(
        [
            result(before=ModelSnapshot(False), after=ModelSnapshot(True)),
            result(ok=False, message="inspection failed"),
            result(ok=False, message="new part failed"),
        ]
    )
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(
        Plan(
            "task",
            [
                ToolCall("session.ensure", {}),
                ToolCall("model.inspect", {}),
                ToolCall("document.new_part", {}),
            ],
        )
    )
    engine.execute_next()
    engine.execute_next()
    engine.skip_failed()
    engine.execute_next()
    context = engine.recovery_context()
    assert [step["tool"] for step in context["completed_steps"]] == ["session.ensure"]
    assert [step["tool"] for step in context["skipped_steps"]] == ["model.inspect"]
    engine.replace_remaining(
        [
            ToolCall("document.new_part", {}),
            ToolCall("selection.plane", {"plane": "top"}),
        ]
    )
    assert [step.tool for step in engine.plan.steps[2:]] == [
        "document.new_part",
        "selection.plane",
    ]
    assert engine.skipped == {1}


def test_failed_profile_geometry_cannot_be_skipped_when_feature_depends_on_it(tmp_path):
    worker = FakeWorker(
        [
            result(
                before=ModelSnapshot(document_type="none"),
                after=ModelSnapshot(document_type="part", document_title="Part1"),
            ),
            result(
                before=ModelSnapshot(document_type="part"),
                after=ModelSnapshot(document_type="part", active_sketch="<active-sketch>"),
            ),
            result(ok=False, message="circle failed"),
        ]
    )
    engine = _engine(tmp_path, worker, [])
    engine.load_plan(
        Plan(
            "dependent geometry",
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "top"}),
                ToolCall("sketch.circle", {"radius_mm": 2}),
                ToolCall("feature.extrude", {"depth_mm": 30}),
            ],
        )
    )
    engine.execute_next()
    engine.execute_next()
    engine.execute_next()
    with pytest.raises(ToolError, match="必要依赖"):
        engine.skip_failed()
