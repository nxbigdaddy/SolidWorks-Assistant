from __future__ import annotations

import copy

import pytest

from sw_assistant.agent.task_backend import TaskAgentBackend
from sw_assistant.infrastructure import ModelSettings
from test_task_checkpoints import KEY, RecoveryWorker, host_task
from test_task_runtime import make_controller, wait_idle


def offline_controller(tmp_path, monkeypatch):
    controller = make_controller(tmp_path / "app")
    seeded, _, _ = host_task(tmp_path / "seed")
    controller.continuous.store.save(seeded.snapshot())
    worker = RecoveryWorker()
    model_calls = []

    def unexpected_model(*args, **kwargs):
        model_calls.append(True)
        raise AssertionError("offline recovery must not request a model")

    monkeypatch.setattr(TaskAgentBackend, "decide", unexpected_model)
    monkeypatch.setattr(controller.worker, "call", worker.call)
    monkeypatch.setattr(controller.worker, "checkpoint_request", worker.checkpoint_request)
    monkeypatch.setattr(controller, "_task_preflight", lambda _call: (True, ""))
    return controller, worker, model_calls, seeded.snapshot()["id"]


@pytest.mark.parametrize("settings", [ModelSettings(), ModelSettings(api_base="incomplete-provider")])
def test_task_record_list_and_explicit_restore_need_no_model_configuration(tmp_path, monkeypatch, settings):
    controller, worker, model_calls, task_id = offline_controller(tmp_path, monkeypatch)
    controller.settings.model = settings
    service = controller.continuous
    try:
        service.restore(task_id)
        wait_idle(service)
        assert service.snapshot()["status"] == "review", service.snapshot()["message"]
        assert worker.calls == [] and worker.rpc == []
        service.refresh_checkpoints()
        wait_idle(service)
        assert service.snapshot()["checkpoint_catalog"][0]["host_prefix_available"] is True
        service.restore_checkpoint(KEY)
        wait_idle(service)
        assert service.snapshot()["status"] == "ready", service.snapshot()["message"]
        assert worker.calls == ["session.ensure", "model.observe"]
        assert [item[0] for item in worker.rpc] == ["list", "restore"]
        assert model_calls == []
    finally:
        controller.shutdown()


def test_stopped_offline_runner_can_be_rebuilt_for_local_checkpoint_list(tmp_path, monkeypatch):
    controller, worker, model_calls, task_id = offline_controller(tmp_path, monkeypatch)
    service = controller.continuous
    try:
        service.restore(task_id)
        wait_idle(service)
        previous = service.runner
        service.stop()
        service.refresh_checkpoints()
        wait_idle(service)
        assert service.runner is not previous
        assert service.snapshot()["status"] == "review"
        assert service.snapshot()["checkpoint_catalog"]
        assert model_calls == [] and worker.calls == []
    finally:
        controller.shutdown()


@pytest.mark.parametrize("operation", ["run", "step", "resume_and_run", "resume_requirements_and_run"])
def test_execution_after_offline_restore_still_requires_model_before_cad(tmp_path, monkeypatch, operation):
    controller, worker, model_calls, task_id = offline_controller(tmp_path, monkeypatch)
    service = controller.continuous
    try:
        service.restore(task_id)
        wait_idle(service)
        method = getattr(service, operation)
        method("继续并保留全部目标") if "resume" in operation else method()
        wait_idle(service)
        assert service.snapshot()["status"] == "failed"
        assert "模型服务" in service.snapshot()["message"]
        assert worker.calls == [] and worker.rpc == [] and model_calls == []
    finally:
        controller.shutdown()


def test_new_configuration_is_bound_without_restarting_restored_cad_worker(tmp_path, monkeypatch):
    controller, worker, model_calls, task_id = offline_controller(tmp_path, monkeypatch)
    service = controller.continuous
    try:
        service.restore(task_id)
        wait_idle(service)
        service.restore_checkpoint(KEY)
        wait_idle(service)
        runner = service.runner
        assert runner is not None
        previous_worker_calls = list(worker.calls)
        previous_prefix = copy.deepcopy(runner.snapshot()["completed_steps"])
        new_settings = ModelSettings(api_base="http://127.0.0.1:9/v1", model="local-fixture")
        controller.settings.model = new_settings
        runs = []
        monkeypatch.setattr(runner, "run", lambda **kwargs: runs.append(kwargs))
        service.run()
        wait_idle(service)
        assert service.runner is runner
        assert runner.backend.settings is new_settings
        assert runs == [{"single_stage": False}]
        assert worker.calls == previous_worker_calls
        assert runner.snapshot()["completed_steps"] == previous_prefix
        assert not runner._cancelled and model_calls == []
    finally:
        controller.shutdown()


def test_normal_runner_creation_keeps_configuration_validation(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    try:
        with pytest.raises(ValueError, match="模型服务"):
            controller.continuous._new_runner("new")
        runner = controller.continuous._new_runner("offline", offline_recovery=True)
        assert runner.backend._pi is None
        assert runner.backend._round == 0
        runner.stop()
    finally:
        controller.shutdown()
