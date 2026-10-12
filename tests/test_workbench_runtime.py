from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from sw_assistant.infrastructure import ModelSettings
from test_task_runtime import make_controller as make_unconfigured_controller, wait_idle


def make_controller(tmp_path):
    owner = make_unconfigured_controller(tmp_path)
    # These tests exercise local StubRunner orchestration, not provider setup.
    # Keep the shared unconfigured fixture unchanged for configuration tests.
    owner.settings.model = ModelSettings(api_base="http://127.0.0.1:9/v1", model="synthetic-workbench")
    return owner


class StubRunner:
    def __init__(self, owner, identifier="stub", *, on_prepare=None, on_run=None):
        self.owner = owner
        self.state = {"id": identifier, "status": "idle"}
        self.calls = []
        self.on_prepare = on_prepare
        self.on_run = on_run

    def snapshot(self):
        return dict(self.state)

    def prepare(self, prompt, context):
        self.calls.append(("prepare", prompt))
        self.state.update(status="ready", prompt=prompt)
        if self.on_prepare:
            self.on_prepare()
        return self.snapshot()

    def run(self, single_stage=False):
        self.calls.append(("run", single_stage))
        worker = self.owner.worker
        assert worker._operation_local.generation == worker._generation
        self.state["status"] = "running"
        if self.on_run:
            self.on_run()
        self.state["status"] = "completed"
        return self.snapshot()

    def steer(self, note):
        self.calls.append(("steer", note))
        if self.state["status"] == "completed":
            self.state["status"] = "ready"
        return self.snapshot()

    def stop(self):
        self.calls.append(("stop", None))
        if self.state["status"] != "completed":
            self.state["status"] = "stopped"
        self.owner.worker.interrupt()

    def restore(self, task_id):
        self.calls.append(("restore", task_id))
        self.state.update(id=task_id, status="review")
        return self.snapshot()

    def resume(self, note, *, as_requirement=False):
        self.calls.append(("resume", note))
        self.state["feedback_as_requirement"] = as_requirement
        self.state["status"] = "ready"
        return self.snapshot()


def test_one_click_start_runs_and_second_task_has_fresh_cancellation_scope(tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    created = []

    def factory(identifier):
        runner = StubRunner(owner, identifier)
        created.append(runner)
        return runner

    monkeypatch.setattr(owner.continuous, "_new_runner", factory)
    try:
        owner.continuous.start("a generic body")
        wait_idle(owner.continuous)
        assert owner.continuous.snapshot()["status"] == "completed"
        owner.continuous.start("a second independent shape")
        wait_idle(owner.continuous)
        assert owner.continuous.snapshot()["status"] == "completed"
        assert [name for name, _ in created[1].calls] == ["prepare", "run"]
        assert owner._active_background is None
    finally:
        owner.shutdown()


def test_stop_at_prepare_completion_never_autoruns(tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    runner = StubRunner(owner, on_prepare=owner.continuous.stop)
    monkeypatch.setattr(owner.continuous, "_new_runner", lambda _: runner)
    try:
        owner.continuous.start("shape")
        wait_idle(owner.continuous)
        assert not any(name == "run" for name, _ in runner.calls)
        assert owner.continuous.snapshot()["status"] == "stopped"
    finally:
        owner.shutdown()


def test_steering_during_run_only_queues_and_completed_followup_runs(tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    entered, release = threading.Event(), threading.Event()
    runner = StubRunner(owner, on_run=lambda: (entered.set(), release.wait(3)))
    monkeypatch.setattr(owner.continuous, "_new_runner", lambda _: runner)
    try:
        owner.continuous.start("body")
        assert entered.wait(2)
        owner.continuous.steer("include a mounting feature")
        assert sum(name == "run" for name, _ in runner.calls) == 1
        assert ("steer", "include a mounting feature") in runner.calls
        release.set()
        wait_idle(owner.continuous)
        owner.continuous.steer("now add a second feature")
        wait_idle(owner.continuous)
        assert sum(name == "run" for name, _ in runner.calls) == 2
    finally:
        release.set()
        owner.shutdown()


def test_resume_stopped_task_and_run_share_fresh_scope(tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    previous, replacement = StubRunner(owner), StubRunner(owner)
    previous.state["status"] = "stopped"
    owner.continuous.runner = previous
    monkeypatch.setattr(owner.continuous, "_new_runner", lambda _: replacement)
    try:
        owner.continuous.resume_and_run("checked the document and restored the previous state")
        wait_idle(owner.continuous)
        assert [name for name, _ in replacement.calls] == ["restore", "resume", "run"]
        assert owner.continuous.snapshot()["status"] == "completed"
    finally:
        owner.shutdown()


def test_steering_queued_at_last_boundary_is_not_stranded(tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    finished, release = threading.Event(), threading.Event()

    class LastBoundaryRunner(StubRunner):
        def run(self, single_stage=False):
            self.calls.append(("run", single_stage))
            self.state["status"] = "completed"
            if sum(name == "run" for name, _ in self.calls) == 1:
                finished.set()
                release.wait(3)
            return self.snapshot()

    runner = LastBoundaryRunner(owner)
    monkeypatch.setattr(owner.continuous, "_new_runner", lambda _: runner)
    try:
        owner.continuous.start("body")
        assert finished.wait(2)
        owner.continuous.steer("additional user requirement")
        release.set()
        deadline = time.monotonic() + 3
        while sum(name == "run" for name, _ in runner.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        wait_idle(owner.continuous)
        assert sum(name == "run" for name, _ in runner.calls) == 2
    finally:
        release.set()
        owner.shutdown()


def test_review_feedback_can_explicitly_add_requirements_and_continue(tmp_path):
    owner = make_controller(tmp_path)
    runner = StubRunner(owner)
    runner.state["status"] = "review"
    owner.continuous.runner = runner
    try:
        owner.continuous.resume_requirements_and_run("add a 5 mm mounting hole")
        wait_idle(owner.continuous)
        assert [name for name, _ in runner.calls] == ["resume", "run"]
        assert owner.continuous.snapshot()["feedback_as_requirement"] is True
        assert owner.continuous.snapshot()["status"] == "completed"
    finally:
        owner.shutdown()


def test_engine_snapshot_uses_actual_backend_and_preserves_public_fallback_reason(tmp_path):
    owner = make_controller(tmp_path)
    runner = StubRunner(owner)
    runner.backend = SimpleNamespace(backend_source="pi")
    service = owner.continuous
    service.runner = runner
    try:
        state = service.snapshot()
        assert state["engine_source"] == "pi"
        assert state["model_response_timeout_seconds"] == owner.settings.model.timeout_seconds
        assert state["engine_fallback_reason"] == ""
        runner.backend.backend_source = "pydantic"
        service._event("agent_fallback", {
            "task_id": "stub", "backend": "pydantic", "message": "切换推理引擎",
            "reason": "Pi 未在响应空闲时限内返回有效内容。",
        })
        service._event("agent_reasoning", {"task_id": "stub", "backend": "pydantic", "message": "继续调查"})
        state = service.snapshot()
        assert state["engine_source"] == "pydantic"
        assert "响应空闲时限" in state["engine_fallback_reason"]
        runner.state["status"] = "review"
        service.resume_and_run("继续")
        wait_idle(service)
        assert service.snapshot()["feedback_as_requirement"] is False
    finally:
        owner.shutdown()


def test_new_task_resets_engine_fallback_and_rejects_old_task_backend_events(tmp_path, monkeypatch):
    owner = make_controller(tmp_path)
    service = owner.continuous
    old = StubRunner(owner, "old")
    old.backend = SimpleNamespace(backend_source="pydantic")
    service.runner = old
    replacement = StubRunner(owner, "new")
    replacement.backend = SimpleNamespace(backend_source="pi")
    monkeypatch.setattr(service, "_new_runner", lambda _: replacement)
    try:
        service._event("agent_fallback", {"task_id": "old", "reason": "旧任务已切换引擎"})
        assert service.snapshot()["engine_fallback_reason"]
        service._replace_runner("new", service._generation)
        service._event("agent_fallback", {"task_id": "old", "reason": "旧进程晚到消息"})
        state = service.snapshot()
        assert state["id"] == "new"
        assert state["engine_source"] == "pi"
        assert state["engine_fallback_reason"] == ""
    finally:
        owner.shutdown()


@pytest.mark.parametrize("operation", ["start", "resume_and_run"])
def test_user_stop_during_runner_replacement_cannot_be_washed_by_new_worker_scope(tmp_path, monkeypatch, operation):
    owner = make_controller(tmp_path)
    service = owner.continuous
    old = StubRunner(owner, "old")
    old.state["status"] = "stopped"
    service.runner = old
    entered, release = threading.Event(), threading.Event()
    replacement = StubRunner(owner, "replacement")

    def factory(_identifier):
        entered.set()
        release.wait(3)
        return replacement

    monkeypatch.setattr(service, "_new_runner", factory)
    try:
        getattr(service, operation)("explicit new task or verified resume")
        assert entered.wait(2)
        service.stop()
        stopped_generation = owner.worker._generation
        release.set()
        wait_idle(service)
        assert not any(name in {"prepare", "resume", "run"} for name, _ in replacement.calls)
        assert service.snapshot()["status"] == "stopped"
        assert owner.worker._generation >= stopped_generation
        assert owner._active_background is None
    finally:
        release.set()
        owner.shutdown()


@pytest.mark.parametrize("operation", ["start", "resume_and_run"])
def test_stop_immediately_after_new_scope_binding_never_executes_task(tmp_path, monkeypatch, operation):
    from contextlib import contextmanager

    owner = make_controller(tmp_path)
    service = owner.continuous
    old, replacement = StubRunner(owner, "old"), StubRunner(owner, "replacement")
    old.state["status"] = "stopped"
    service.runner = old
    monkeypatch.setattr(service, "_new_runner", lambda _identifier: replacement)
    original_operation = owner.worker.operation

    @contextmanager
    def stop_at_scope():
        with original_operation():
            service.stop()
            yield

    monkeypatch.setattr(owner.worker, "operation", stop_at_scope)
    try:
        getattr(service, operation)("explicit request")
        wait_idle(service)
        assert not any(name in {"prepare", "resume", "run"} for name, _ in replacement.calls)
        assert service.snapshot()["status"] == "stopped"
        assert owner._active_background is None
    finally:
        owner.shutdown()
