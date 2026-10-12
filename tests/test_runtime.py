from __future__ import annotations

import json
import sqlite3
import threading
import time

from PySide6.QtCore import Qt

from sw_assistant.domain import (
    ExecutionState,
    ModelSnapshot,
    Plan,
    PlanningError,
    StepResult,
    ToolCall,
)
from sw_assistant.infrastructure import AppPaths
from sw_assistant.runtime import ApplicationController
from sw_assistant.solidworks import AddinEvent


def _paths(tmp_path):
    return AppPaths(
        data=tmp_path,
        logs=tmp_path / "logs",
        settings=tmp_path / "settings.json",
        library_db=tmp_path / "library.db",
        history_db=tmp_path / "history.db",
    )


def test_library_background_tasks_are_mutually_exclusive(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    started = threading.Event()
    release = threading.Event()
    events = []
    controller.activity.connect(lambda name, payload: events.append((name, payload)))

    def blocking_scan(*_args, **_kwargs):
        started.set()
        release.wait(2)
        return {
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": False,
        }

    monkeypatch.setattr(controller.library, "scan", blocking_scan)
    try:
        controller.scan_library_async(str(tmp_path))
        assert started.wait(1)
        controller.scan_library_async(str(tmp_path))
        assert any(name == "library_rejected" for name, _ in events)
        assert controller._active_background is not None
        release.set()
        deadline = time.monotonic() + 2
        while controller._active_background is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller._active_background is None
    finally:
        release.set()
        controller.shutdown()


def test_reset_interrupts_worker_before_clearing_plan(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    interrupted = []
    monkeypatch.setattr(controller.worker, "interrupt", lambda: interrupted.append(True))
    plan = Plan("task", [ToolCall("session.ensure", {})])
    controller.history.save_plan("task", plan)
    controller.engine.load_plan(plan)
    try:
        controller.reset()
        assert interrupted == [True]
        deadline = time.monotonic() + 1
        while controller.engine.plan is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller.engine.plan is None
        assert controller.history.recent()[0]["state"] == "aborted"
    finally:
        controller.shutdown()


def test_library_analysis_runs_off_ui_thread_and_emits_result(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    completed = threading.Event()
    events = []
    controller.activity.connect(
        lambda name, payload: (
            events.append((name, payload)),
            completed.set() if name == "analysis_succeeded" else None,
        ),
        Qt.ConnectionType.DirectConnection,
    )
    monkeypatch.setattr(
        controller.library_analyzer,
        "compare",
        lambda left, right: {"left": left, "right": right, "equal": False},
    )
    try:
        controller.compare_documents_async(1, 2)
        assert completed.wait(2)
        names = [name for name, _payload in events]
        assert names[:2] == ["analysis_started", "analysis_succeeded"]
        assert events[1][1]["result"]["right"] == 2
    finally:
        controller.shutdown()


def test_addin_command_events_create_review_only_demonstration_recipe(tmp_path):
    controller = ApplicationController(_paths(tmp_path))

    def event(sequence, name, payload):
        return AddinEvent(
            1,
            sequence,
            "session",
            f"event-{sequence}",
            "2026-08-10T00:00:00+00:00",
            100,
            name,
            payload,
        )

    context = {
        "captured_at": "2099-01-01T00:00:00+00:00",
        "process_id": 100,
        "solidworks_revision": "33.5.0",
        "document": {
            "document_type": 1,
            "title": "Part1",
            "path": str(tmp_path / "Part1.SLDPRT"),
        },
        "selections": [{"index": 1, "selection_type": 2, "name": "Face1"}],
    }
    try:
        controller._on_addin_event(event(1, "context.changed", {"context": context}))
        controller.start_demonstration("拉伸示教")
        controller._on_addin_event(
            event(2, "command.started", {"command": 100, "context": context})
        )
        changed = {**context, "document": {**context["document"], "feature_count": 2}}
        controller._on_addin_event(
            event(
                3,
                "command.finished",
                {"command": 100, "reason": 0, "context": changed},
            )
        )
        recipe = controller.finish_demonstration()
        assert recipe["review_state"] == "draft"
        assert recipe["steps"][0]["command_id"] == 100
        approved = controller.approve_demonstration_recipe(recipe["id"])
        assert approved["review_state"] == "approved"
        replay = controller.prepare_demonstration_replay(recipe["id"])
        assert replay["mode"] == "guided"
        recorded = controller.record_demonstration_replay_result(recipe["id"], succeeded=True)
        assert recorded["success_count"] == 1
    finally:
        controller.shutdown()


def test_stop_all_cancels_execution_background_work_and_persistent_jobs(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    interrupted = []
    events = []
    controller.activity.connect(
        lambda name, payload: events.append((name, payload)),
        Qt.ConnectionType.DirectConnection,
    )
    monkeypatch.setattr(controller.worker, "interrupt", lambda: interrupted.append(True))
    first = controller.jobs.create("health.inspect", [{"document_id": 1}])
    second = controller.jobs.create("semantic.reindex", [{"document_id": 2}])
    try:
        controller.abort_all()
        assert interrupted == [True]
        assert controller._cancel.is_set()
        assert controller.batch_runner.cancel.is_set()
        assert controller.jobs.get(first.id).state == "cancelled"
        assert controller.jobs.get(second.id).state == "cancelled"
        assert any(name == "all_cancel_requested" for name, _payload in events)
    finally:
        controller.shutdown()


def test_document_write_preflight_uses_fresh_readonly_context(tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    event = AddinEvent(
        1,
        1,
        "session",
        "event-1",
        "2099-01-01T00:00:00+00:00",
        100,
        "context.changed",
        {
            "context": {
                "captured_at": "2099-01-01T00:00:00+00:00",
                "process_id": 100,
                "solidworks_revision": "33.5.0",
                "command_in_progress": False,
                "document": {
                    "document_type": 1,
                    "title": "Part1",
                    "read_only": True,
                },
            }
        },
    )
    try:
        controller._on_addin_event(event)
        controller.engine.results.append(
            StepResult(
                True,
                ToolCall("document.new_part", {}),
                "ok",
                after=ModelSnapshot(document_type="part", document_title="Part1"),
            )
        )
        allowed, message = controller._execution_preflight(
            ToolCall("sketch.circle", {"radius_mm": 1})
        )
        assert allowed is False
        assert "只读" in message
        assert controller._execution_preflight(ToolCall("document.new_part", {}))[0] is True
    finally:
        controller.shutdown()


def test_other_recovery_action_invalidates_pending_model_repair(tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    try:
        request_id = controller._next_plan_request()
        controller.retry_failed()
        assert controller._plan_current(request_id) is False
    finally:
        controller.shutdown()


def test_geometry_failure_automatically_requests_validated_repair(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    stable = ModelSnapshot(
        connected=True,
        document_title="Part1",
        document_type="part",
        active_sketch="Sketch1",
    )
    controller.settings.model.api_base = "https://example.com/v1"
    controller.settings.model.model = "test-model"
    controller.settings.auto_repair_failures = True
    controller.settings.auto_resume_after_repair = True
    controller.engine.plan = Plan("task", [ToolCall("sketch.polygon", {"radius_mm": 2})])
    controller.engine.state = ExecutionState.PAUSED
    controller.engine.results = [
        StepResult(
            False,
            controller.engine.plan.steps[0],
            "SolidWorks 创建正多边形失败；已自动回滚本次新增草图实体。",
            error_code="ToolError",
            before=stable,
            after=stable,
        )
    ]
    controller._run_all_requested = True
    scheduled = []
    monkeypatch.setattr(
        controller,
        "repair_failed_async",
        lambda **arguments: scheduled.append(arguments),
    )
    try:
        controller._schedule_automatic_recovery()
        assert scheduled == [{"automatic": True, "resume_after": True}]
        assert controller._automatic_repair_total == 1
    finally:
        controller.shutdown()


def test_failed_automatic_replacement_releases_recovery_lock(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    completed = threading.Event()
    request_id = controller._next_plan_request()
    controller._automatic_recovery_active = True
    controller.activity.connect(
        lambda name, _payload: completed.set() if name == "runtime_error" else None,
        Qt.ConnectionType.DirectConnection,
    )

    def fail_replacement(_calls):
        raise RuntimeError("replacement rejected")

    monkeypatch.setattr(controller.engine, "replace_remaining", fail_replacement)
    try:
        controller._commands.put(
            ("replace_remaining", (request_id, [], True, False))
        )
        assert completed.wait(2)
        assert controller._automatic_recovery_active is False
    finally:
        controller.shutdown()


def test_superseded_automatic_repair_releases_recovery_lock(tmp_path, monkeypatch):
    controller = ApplicationController(_paths(tmp_path))
    controller.settings.agent_enabled = False
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(controller.engine, "recovery_context", lambda: {})

    def delayed_repair(_self, _context, _progress):
        started.set()
        release.wait(2)
        return Plan("replacement", [ToolCall("session.ensure", {})])

    monkeypatch.setattr("sw_assistant.runtime.PlannerService.repair", delayed_repair)
    try:
        controller.repair_failed_async(automatic=True)
        assert started.wait(1)
        controller._invalidate_plan()
        release.set()
        deadline = time.monotonic() + 2
        while controller._automatic_recovery_active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller._automatic_recovery_active is False
    finally:
        release.set()
        controller.shutdown()


def test_repair_uses_bounded_supervisor_when_enabled(tmp_path, monkeypatch):
    from sw_assistant.agent import (
        SupervisorAction,
        SupervisorDecision,
        SupervisorOutcome,
    )

    controller = ApplicationController(_paths(tmp_path))
    controller.settings.agent_enabled = True
    controller.settings.agent_max_model_requests = 6
    controller.settings.agent_backend = "pydantic"
    controller.settings.model.api_base = "https://example.com/v1"
    controller.settings.model.model = "test-model"
    captured = []
    applied = threading.Event()
    monkeypatch.setattr(
        controller.engine,
        "recovery_context",
        lambda: {"recovery_assessment": {"state_known": True}},
    )

    def supervise(service, context, progress=None):
        captured.append((service.max_model_requests, context))
        decision = SupervisorDecision(
            action=SupervisorAction.REPLACE_REMAINING,
            reason="validated replacement",
            replacement_steps=[
                {
                    "tool": "session.ensure",
                    "arguments": {},
                    "description": "reconnect",
                }
            ],
            expected_postconditions=["connected"],
            confidence=0.9,
        )
        return SupervisorOutcome(decision, Plan("repair", [ToolCall("session.ensure", {})]))

    monkeypatch.setattr("sw_assistant.runtime.SupervisorService.supervise", supervise)
    monkeypatch.setattr(
        controller.engine,
        "replace_remaining",
        lambda calls: applied.set(),
    )
    try:
        controller.repair_failed_async()
        assert applied.wait(2)
        assert captured[0][0] == 6
        assert captured[0][1]["automatic_recovery"] is False
    finally:
        controller.shutdown()


def test_pi_supervisor_is_reused_for_the_same_plan(tmp_path, monkeypatch):
    from sw_assistant.agent import (
        SupervisorAction,
        SupervisorDecision,
        SupervisorOutcome,
    )

    controller = ApplicationController(_paths(tmp_path))
    controller.settings.agent_backend = "pi"
    controller.settings.model.api_base = "https://example.com/v1"
    controller.settings.model.model = "test-model"
    controller.engine.plan = Plan("task", [ToolCall("session.ensure", {})])
    instances = []

    def supervise(service, _context, _progress=None):
        instances.append(service)
        decision = SupervisorDecision(
            action=SupervisorAction.REQUEST_REVIEW,
            reason="review",
            confidence=0.5,
        )
        return SupervisorOutcome(decision, None)

    monkeypatch.setattr("sw_assistant.runtime.PiSupervisorService.supervise", supervise)
    try:
        first, first_backend = controller._supervise_recovery({}, None)
        second, second_backend = controller._supervise_recovery({}, None)
        assert first.plan is None and second.plan is None
        assert first_backend == second_backend == "pi"
        assert instances[0] is instances[1]
    finally:
        controller.shutdown()


def test_auto_backend_falls_back_once_per_plan(tmp_path, monkeypatch):
    from sw_assistant.agent import (
        SupervisorAction,
        SupervisorDecision,
        SupervisorOutcome,
    )

    controller = ApplicationController(_paths(tmp_path))
    controller.settings.agent_backend = "auto"
    controller.settings.model.api_base = "https://example.com/v1"
    controller.settings.model.model = "test-model"
    controller.engine.plan = Plan("task", [ToolCall("session.ensure", {})])
    pi_attempts = []
    events = []

    def unavailable():
        pi_attempts.append(True)
        raise RuntimeError("Pi unavailable")

    def fallback(_service, _context, _progress=None):
        decision = SupervisorDecision(
            action=SupervisorAction.REQUEST_REVIEW,
            reason="safe fallback",
            confidence=0.5,
        )
        return SupervisorOutcome(decision, None)

    monkeypatch.setattr(controller, "_get_pi_supervisor", unavailable)
    monkeypatch.setattr("sw_assistant.runtime.SupervisorService.supervise", fallback)
    controller.activity.connect(lambda name, _payload: events.append(name))
    try:
        assert controller._supervise_recovery({}, None)[1] == "pydantic"
        assert controller._supervise_recovery({}, None)[1] == "pydantic"
        assert len(pi_attempts) == 1
        assert events.count("pi_agent_fallback") == 1
    finally:
        controller.shutdown()


def test_initial_planning_uses_bounded_agent_and_preserves_assumptions(
    tmp_path, monkeypatch
):
    controller = ApplicationController(_paths(tmp_path))
    controller.settings.agent_enabled = True
    controller.settings.agent_max_model_requests = 7
    controller.settings.model.api_base = "https://example.com"
    controller.settings.model.model = "test-model"
    completed = threading.Event()
    received = []
    captured = []
    controller.plan_ready.connect(
        lambda plan: (received.append(plan), completed.set()),
        Qt.ConnectionType.DirectConnection,
    )

    def create_plan(service, prompt, runtime_context=None, progress=None):
        captured.append((service.max_model_requests, prompt, runtime_context))
        if progress:
            progress("agent_completed", "agent planned", {"assumption_count": 1})
        return Plan(
            "手机近似主体",
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.rectangle", {"width_mm": 160, "height_mm": 78}),
                ToolCall("feature.extrude", {"depth_mm": 9}),
            ],
            assumptions=["使用近似包络尺寸。"],
        )

    monkeypatch.setattr(
        "sw_assistant.planning.planner.RequirementsAgentService.plan",
        create_plan,
    )
    try:
        controller.plan_async("画一个 iPhone 手机模型")
        assert completed.wait(2)
        assert captured[0][0] == 7
        assert captured[0][2] == {"available": False}
        assert received[0].assumptions == ["使用近似包络尺寸。"]
    finally:
        controller.shutdown()


def test_planning_failure_emits_and_persists_structured_diagnostics(
    tmp_path, monkeypatch
):
    controller = ApplicationController(_paths(tmp_path))
    completed = threading.Event()
    events = []
    controller.activity.connect(
        lambda name, payload: (
            events.append((name, payload)),
            completed.set() if name == "planning_failed" else None,
        ),
        Qt.ConnectionType.DirectConnection,
    )
    failed_plan = Plan(
        "bad plane",
        [
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "left"}),
        ],
    )

    def fail_plan(self, prompt, progress=None):
        raise PlanningError(
            "第 2 步平面无效",
            details={
                "step_index": 2,
                "tool": "sketch.begin",
                "field": "plane",
                "received_value": "left",
                "expected_schema": {
                    "type": "string",
                    "enum": ["front", "top", "right"],
                },
                "failed_plan": failed_plan.to_dict(),
            },
        )

    monkeypatch.setattr("sw_assistant.runtime.PlannerService.plan", fail_plan)
    try:
        controller.plan_async("create part")
        assert completed.wait(2)
        failure = next(payload for name, payload in events if name == "planning_failed")
        assert failure["diagnostics"]["step_index"] == 2
        assert failure["diagnostics"]["received_value"] == "left"
        assert "failed_plan" not in failure["diagnostics"]
        assert failure["failure_id"].startswith("planning-failed-")

        with sqlite3.connect(tmp_path / "history.db") as db:
            payload = json.loads(
                db.execute(
                    "SELECT plan_json FROM plan_history WHERE id=?",
                    (failure["failure_id"],),
                ).fetchone()[0]
            )
        assert payload["plan"]["steps"][1]["arguments"]["plane"] == "left"
        assert payload["diagnostics"]["field"] == "plane"
    finally:
        controller.shutdown()


def test_capability_status_rejects_snapshot_from_another_solidworks_process(tmp_path):
    controller = ApplicationController(_paths(tmp_path))
    bridge_dir = tmp_path / "event-bridge"
    bridge_dir.mkdir(exist_ok=True)
    snapshot_path = bridge_dir / "capabilities.json"
    snapshot = {
        "schema_version": 1,
        "detected_at_utc": "2099-01-01T00:00:00+00:00",
        "process_id": 999,
        "solidworks_revision": "33.5.0",
        "capabilities": [{"id": "drawing.generate", "available": True}],
    }
    snapshot_path.write_text(json.dumps(snapshot), "utf-8")

    def addin_event(sequence, name, payload):
        return AddinEvent(
            1,
            sequence,
            "live-session",
            f"event-{sequence}",
            "2099-01-01T00:00:00+00:00",
            100,
            name,
            payload,
        )

    context = {
        "captured_at": "2099-01-01T00:00:00+00:00",
        "process_id": 100,
        "solidworks_revision": "33.5.0",
        "document": {"document_type": 1, "title": "Part1"},
    }
    try:
        controller._on_addin_event(addin_event(1, "context.changed", {"context": context}))
        controller._on_addin_event(
            addin_event(
                2,
                "bridge.connected",
                {"revision": "33.5.0", "taskpane_available": True},
            )
        )
        assert controller._capability_status("drawing.generate").available is False

        snapshot["process_id"] = 100
        snapshot_path.write_text(json.dumps(snapshot), "utf-8")
        assert controller._capability_status("drawing.generate").available is True
    finally:
        controller.shutdown()
