"""Qt boundary for the persistent modelling loop; CAD work stays off the UI thread."""
from __future__ import annotations

import threading
import uuid
import math
from collections.abc import Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from .agent.task_backend import TaskAgentBackend
from .agent.task_runner import ContinuousTaskRunner
from .agent.task_store import TaskStore

if TYPE_CHECKING:
    from .runtime import ApplicationController


class ContinuousTaskController(QObject):
    updated = Signal(object)
    activity = Signal(str, object)

    def __init__(self, owner: ApplicationController) -> None:
        super().__init__(owner)
        self.owner = owner
        self.store = TaskStore(owner.paths.data / "tasks")
        self.runner: ContinuousTaskRunner | None = None
        self._lock = threading.RLock()
        self._busy = False
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        self._last_error = ""
        self._steer_followup = False
        self._engine_fallback_reason = ""
        self._agent_progress_message = ""
        self._agent_progress: dict[str, int] = {}
        self._progress_cancelled = False
        self._runner_offline_recovery = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def snapshot(self) -> dict[str, Any]:
        runner = self.runner
        value = runner.snapshot() if runner else {
            "status": "idle", "message": "输入任务，Agent 将自主规划、调查、执行并验证。"
        }
        if self._last_error:
            value = {**value, "status": "failed", "message": self._last_error}
        source = getattr(getattr(runner, "backend", None), "backend_source", "unknown")
        with self._lock:
            reason = self._engine_fallback_reason
            active = (value.get("status") == "running"
                      or (self._busy and value.get("status") == "ready"))
            active = active and not self._progress_cancelled and not self._closed
            progress_message = self._agent_progress_message if active else ""
            progress = dict(self._agent_progress) if active else {}
        return {
            **value, "busy": self.busy,
            "engine_source": source if source in {"pi", "pydantic"} else "unknown",
            "engine_fallback_reason": reason,
            "model_response_timeout_seconds": self.owner.settings.model.timeout_seconds,
            "agent_progress_message": progress_message,
            "agent_progress": progress,
        }

    def recent(self) -> list[dict[str, Any]]:
        return self.store.recent(limit=30)

    def _validate_model(self) -> None:
        self.owner.settings.model.validate()
        if not self.owner.settings.model.configured:
            raise ValueError("请先在设置 → 模型服务中填写地址、模型和 API Key。")

    def _require_execution_model(self) -> None:
        self._validate_model()
        if self._runner_offline_recovery and self.runner is not None:
            # Offline recovery never starts an inference session. Pick up a
            # newly configured provider without interrupting the CAD worker and
            # losing ownership of the just-restored native working copy.
            if isinstance(self.runner.backend, TaskAgentBackend):
                self.runner.backend.settings = self.owner.settings.model
            self._runner_offline_recovery = False

    def _new_runner(self, session_id: str, *, offline_recovery: bool = False) -> ContinuousTaskRunner:
        if not offline_recovery:
            self._validate_model()
        backend = TaskAgentBackend(
            self.owner.settings.model,
            self.owner.registry,
            self.owner.paths.data / "task-agent" / session_id,
            session_id=session_id,
            backend=self.owner.settings.agent_backend,
            on_event=lambda name, message, details: self._event(
                name, {"message": message, **details, "task_id": session_id}
            ),
        )
        runner = ContinuousTaskRunner(
            self.owner.registry,
            self.owner.worker,
            backend,
            self.store,
            on_event=self._event,
            preflight=self.owner._task_preflight,
            max_rounds=48,
            task_id=session_id,
        )
        backend.set_investigator(runner.investigate)
        backend.set_operator(runner.execute_agent_stage)
        return runner

    def prepare(self, prompt: str) -> None:
        self._prepare(prompt, auto_run=False)

    def start(self, prompt: str) -> None:
        """One user submission owns preparation and continuous execution."""
        self._prepare(prompt, auto_run=True)

    def _prepare(self, prompt: str, *, auto_run: bool) -> None:
        prompt = str(prompt).strip()
        if not prompt:
            self._event("task_rejected", {"message": "请先输入建模目标。"})
            return

        def work(generation: int) -> None:
            if self.runner is not None:
                self.runner.stop()
            self._replace_runner(uuid.uuid4().hex, generation)
            assert self.runner is not None
            # Stopping the preceding task advances the worker epoch. Bind the
            # new task only after that internal stop, while checking user Stop.
            with self.owner.worker.operation():
                if not self._current(generation):
                    return
                state = self.runner.prepare(prompt, self.owner._planning_agent_context())
                if auto_run and state.get("status") == "ready" and self._current(generation):
                    self.runner.run()

        self._submit("开始自主任务" if auto_run else "仅准备任务", work, bind_worker_scope=False)

    def _current(self, generation: int) -> bool:
        with self._lock:
            return not self._closed and generation == self._generation

    def steer(self, note: str) -> None:
        note = str(note).strip()
        if not note:
            return
        with self._lock:
            runner = self.runner
            closed = self._closed
        if closed or runner is None or not runner.snapshot().get("id"):
            self._event("task_rejected", {"message": "任务尚未建立，请等待开始或重新提交。"})
            return
        state = runner.snapshot()
        if state.get("status") not in {"running", "ready", "completed"}:
            self._event("task_rejected", {"message": "任务需要人工核对，请通过反馈入口继续。"})
            return
        # Never hold the controller lock while calling the runner: a background
        # runner event may be simultaneously requesting a controller snapshot.
        runner.steer(note)
        with self._lock:
            if runner is not self.runner or self._closed:
                return
            busy = self._busy
            self._steer_followup = busy
        self.updated.emit(self.snapshot())
        if not busy:
            self.run()

    def run(self) -> None:
        self._run(False)

    def step(self) -> None:
        self._run(True)

    def _run(self, single_stage: bool) -> None:
        if self.runner is None:
            self._event("task_rejected", {"message": "请先准备或恢复任务。"})
            return
        def work(_generation: int) -> None:
            self._require_execution_model()
            if self.runner is not None:
                self.runner.run(single_stage=single_stage)
        self._submit(
            "执行一个阶段" if single_stage else "持续建模",
            work,
        )

    def resume(self, note: str) -> None:
        self._resume(note, auto_run=False)

    def resume_and_run(self, note: str) -> None:
        self._resume(note, auto_run=True)

    def resume_requirements_and_run(self, note: str) -> None:
        self._resume(note, auto_run=True, as_requirement=True)

    def _resume(self, note: str, *, auto_run: bool, as_requirement: bool = False) -> None:
        if self.runner is None or not str(note).strip():
            self._event("task_rejected", {"message": "请说明已完成的人工操作或补充条件。"})
            return
        def work(generation: int) -> None:
            assert self.runner is not None
            if auto_run:
                self._require_execution_model()
            state = self.runner.snapshot()
            if state.get("status") in {"stopped", "failed", "interrupted"}:
                self.runner.stop()
                self._replace_runner(str(state["id"]), generation)
                self.runner.restore(str(state["id"]))
            with self.owner.worker.operation():
                if not self._current(generation):
                    return
                state = self.runner.resume(note, as_requirement=True) if as_requirement else self.runner.resume(note)
                if auto_run and state.get("status") == "ready" and self._current(generation):
                    self.runner.run()

        self._submit("核对反馈并继续" if auto_run else "核对人工反馈", work, bind_worker_scope=False)

    def restore(self, task_id: str) -> None:
        if not str(task_id).strip():
            return

        def work(generation: int) -> None:
            # Reading a record never executes its pending CAD operations.
            if self.store.load(task_id) is None:
                raise ValueError("任务记录不存在。")
            if self.runner is not None:
                self.runner.stop()
            self._replace_runner(task_id, generation, offline_recovery=True)
            assert self.runner is not None
            self.runner.restore(task_id)

        self._submit("恢复任务记录", work)

    def _checkpoint_runner(self, generation: int) -> ContinuousTaskRunner:
        runner = self.runner
        if runner is None or not runner.snapshot().get("id"):
            raise ValueError("请先选择并恢复任务记录。")
        state = runner.snapshot()
        if getattr(runner, "_cancelled", False):
            # Explicit new user action, not a late callback reviving Stop.
            self._replace_runner(str(state["id"]), generation, offline_recovery=True)
            assert self.runner is not None
            self.runner.restore(str(state["id"]))
            runner = self.runner
        return runner

    def refresh_checkpoints(self) -> None:
        def work(generation: int) -> None:
            runner = self._checkpoint_runner(generation)
            with self.owner.worker.operation():
                if self._current(generation):
                    runner.refresh_checkpoints()
        self._submit("校验本任务磁盘检查点", work)

    def restore_checkpoint(self, checkpoint_id: str) -> None:
        def work(generation: int) -> None:
            runner = self._checkpoint_runner(generation)
            with self.owner.worker.operation():
                if self._current(generation):
                    runner.restore_checkpoint(checkpoint_id)
        self._submit("恢复检查点副本（完成后不自动执行）", work)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._steer_followup = False
            self._agent_progress_message = ""
            self._agent_progress = {}
            self._progress_cancelled = True
            runner = self.runner
        if runner is not None:
            runner.stop()
        self.updated.emit(self.snapshot())

    def stop_all(self) -> None:
        self.owner.abort_all()

    def _replace_runner(self, session_id: str, generation: int, *, offline_recovery: bool = False) -> None:
        runner = (self._new_runner(session_id, offline_recovery=True) if offline_recovery
                  else self._new_runner(session_id))
        with self._lock:
            if self._closed or self._generation != generation:
                runner.stop()
                raise InterruptedError("任务已停止。")
            self.runner = runner
            self._runner_offline_recovery = offline_recovery
            self._engine_fallback_reason = ""
            self._agent_progress_message = ""
            self._agent_progress = {}

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _submit(self, label: str, callback: Callable[[int], Any], *, bind_worker_scope: bool = True) -> None:
        task_id = "continuous-" + uuid.uuid4().hex
        with self._lock:
            if self._closed or self._busy:
                self._event("task_rejected", {"message": "当前任务仍在处理，请等待或停止。"})
                return
            if not self.owner._claim_background("continuous_task", task_id):
                self._event("task_rejected", {"message": "已有模型库或执行任务运行中。"})
                return
            self._busy = True
            self._steer_followup = False
            self._last_error = ""
            self._agent_progress_message = ""
            self._agent_progress = {}
            self._progress_cancelled = False
            self._generation += 1
            generation = self._generation
        self.owner._cancel.clear()
        self.updated.emit({**self.snapshot(), "busy": True, "message": label})

        def work() -> None:
            try:
                # prepare/resume explicitly stop a prior runner before binding
                # their own fresh worker scope. An outer scope here would pin
                # the old cancelled epoch; nested scopes must never wash it.
                with self.owner.worker.operation() if bind_worker_scope else nullcontext():
                    with self._lock:
                        if generation != self._generation or self._closed:
                            return
                    callback(generation)
            except Exception as exc:
                if generation == self._generation and not self._closed:
                    self._last_error = str(exc)
                    self._event("task_error", {"message": str(exc)})
                    self.updated.emit({**self.snapshot(), "status": "failed", "message": str(exc)})
            finally:
                with self._lock:
                    self._busy = False
                    followup = self._steer_followup and generation == self._generation and not self._closed
                    self._steer_followup = False
                self.owner._release_background(task_id)
                self.updated.emit(self.snapshot())
                # A steering message can land just as the preceding turn ends.
                # Do not leave that explicit user submission stranded in ready.
                if followup and self.runner and self.runner.snapshot().get("status") == "ready":
                    self.run()

        self._thread = threading.Thread(target=work, name="ContinuousModelling", daemon=True)
        self._thread.start()

    def _event(self, name: str, payload: dict[str, Any]) -> None:
        if name == "cad_progress":
            # The native helper emits public phase summaries only. Ignore any
            # accidental extra fields rather than recording command arguments.
            payload = {key: payload[key] for key in (
                "message", "phase", "request_id", "tool", "operation", "elapsed_seconds",
            ) if key in payload}
            state = self.runner.snapshot() if self.runner else {}
            with self._lock:
                if (not self._busy or self._closed or self._progress_cancelled
                        or state.get("status") not in {"running", "ready"}):
                    return
                self._agent_progress_message = str(payload.get("message") or "SolidWorks 正在处理当前阶段。")[:400]
                elapsed = payload.get("elapsed_seconds")
                if (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                        and math.isfinite(elapsed) and 0 <= elapsed <= 86_400):
                    self._agent_progress_message += f" · 已用 {elapsed:.0f} 秒"
                self._agent_progress = {}
        if name.startswith("agent_") or name == "pi_event":
            task_id = payload.get("task_id")
            if task_id and (self.runner is None or self.runner.snapshot().get("id") != task_id):
                return
            if name == "agent_fallback":
                with self._lock:
                    # The backend supplies the public, credential-redacted reason.
                    self._engine_fallback_reason = str(payload.get("reason") or payload.get("message") or "Pi 会话不可用。")[:2000]
        # Activity is emitted before the snapshot. Retain only host-generated
        # public progress so that the subsequent repaint cannot erase it with
        # the runner's older "analysing requirements" message. No model text,
        # thinking deltas or arbitrary payload fields enter these counters.
        state = self.runner.snapshot() if self.runner else {}
        with self._lock:
            # run() intentionally stays ready during its first guarded native
            # observation/reconnection. Public CAD progress is still useful in
            # this busy startup window; it must not promote state or revive a
            # stopped/reviewed task, or admit late model-response counters.
            active = (state.get("status") == "running"
                      or (name == "cad_progress" and self._busy and state.get("status") == "ready"))
            active = active and not self._progress_cancelled and not self._closed
            if name == "cad_progress" and (not active or not self._busy):
                return  # Stop/state can change after the initial phase check.
            if name in {"agent_compaction_started", "agent_compaction_finished"} and not active:
                return
            if not active or name in {
                "agent_reasoning", "agent_decision", "stage_started", "step_started", "stage_completed",
                "task_preparing", "task_restored", "task_resumed", "task_error",
                "trial_started", "trial_ready", "trial_verifying", "trial_restored", "trial_committed", "trial_step_failed",
                "checkpoint_restore_started", "checkpoint_restored",
            }:
                self._agent_progress_message = ""
                self._agent_progress = {}
            elif name in {"agent_compaction_started", "agent_compaction_finished"}:
                self._agent_progress_message = str(payload.get("message") or "Pi 正在整理会话上下文。")[:400]
                self._agent_progress = {}
            elif name in {"agent_response_progress", "agent_investigation_finished"}:
                raw = payload.get("progress")
                if name == "agent_investigation_finished":
                    # The response event precedes tool execution; refresh reads
                    # after completion so the displayed count does not lag by 1.
                    raw = getattr(getattr(self.runner, "backend", None), "progress", self._agent_progress)
                progress = raw if isinstance(raw, dict) else {}
                self._agent_progress = {
                    key: progress[key] for key in (
                        "decision_rounds", "observed_model_responses", "round_investigation_requests",
                    ) if isinstance(progress.get(key), int) and not isinstance(progress[key], bool)
                    and 0 <= progress[key] <= 1_000_000
                }
                count = self._agent_progress.get("observed_model_responses")
                reads = self._agent_progress.get("round_investigation_requests")
                self._agent_progress_message = (
                    f"模型已返回第 {count} 次响应" if count is not None else "模型已返回新的响应"
                ) + (f"，本轮已调查 {reads} 次" if reads is not None else "") + "；正在形成阶段决策。"
            elif name == "agent_loop_correction":
                self._agent_progress_message = (
                    "上次响应达到输出长度上限，正在保留已完成步骤并请求简短阶段决策。"
                    if payload.get("correction_kind") == "output_truncated"
                    else "本轮调查已结束，正在等待下一阶段决策。"
                    if payload.get("correction_kind") == "investigation_handoff"
                    else "已结束无效候选，正在要求 Agent 提交下一阶段决策。"
                )
        self.owner.audit.write(name, details=payload)
        self.activity.emit(name, payload)
        self.updated.emit(self.snapshot())
