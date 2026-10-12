"""Bounded continuous agent: proposals never bypass local CAD verification."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import uuid
from typing import Any, Callable

from ..domain import ModelSnapshot, StepResult, ToolCall, utc_now
from ..solidworks.client import ToolWorker
from ..tools import ToolRegistry
from .security import scrub_agent_value
from .failure_evidence import failure_geometry_summary
from .failure_policy import (
    LABELS as FAILURE_LABELS, classify as classify_failure, ensure_budget, exhausted as exhausted_failure,
    grant_manual_window, manual_rearm_candidates, new_budget, record_failure,
    remaining as remaining_failures, reset_verified_window,
)
from .task_contract import (
    contract_issues, is_control_message, merge_requirements, migrate_control_requirements,
    observed_target, refine_goals, seed_requirements,
)
from .task_models import GoalCheck, TaskBackend, TaskDecision
from .task_operations import NativeStageRunnerMixin
from .task_store import TaskStore
from .task_trials import TrialRunnerMixin


_READ_TOOLS = {
    "model.observe", "model.inspect", "session.capabilities", "model.references",
    "sketch.inspect", "feature.inspect", "diagnostics.recent", "model.measure_extents",
}
_MODEL_TOOLS = {
    "session.ensure", "document.new_part", "selection.plane", "sketch.begin",
    "sketch.begin_face", "sketch.circle", "sketch.rectangle", "sketch.polygon",
    "sketch.line", "sketch.centerline", "feature.extrude", "feature.revolve",
    "sketch.set_circle_radius",
}
_SNAPSHOT_FIELDS = (
    "connected", "document_title", "document_path", "document_type", "active_sketch",
    "sketch_entity_count", "feature_count", "body_count", "volume_m3",
)
_TERMINAL = {"completed", "stopped", "failed", "interrupted"}
_SOLID_FEATURE_TYPES = {"boss", "bossthin", "cut", "cutthin", "revolution", "revcut"}


def _signature(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _evidence_value(value: Any) -> Any:
    if isinstance(value, dict):
        volatile = {"time", "timestamp", "captured_at", "checked_at", "updated_at", "duration_ms", "created_at"}
        return {key: _evidence_value(item) for key, item in value.items() if key not in volatile}
    if isinstance(value, list):
        return [_evidence_value(item) for item in value]
    return value


def _equivalent(left: Any, right: Any, key: str = "") -> bool:
    """COM mass-property readers can differ by floating-point noise, not topology."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equivalent(left[name], right[name], name) for name in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equivalent(a, b, key) for a, b in zip(left, right))
    if key in {"volume_m3", "value_mm", "radius_mm", "expected_radius_mm", "center_sketch_m"} and all(
        isinstance(item, (float, int)) and not isinstance(item, bool) for item in (left, right)
    ):
        tolerance = 1e-12 if key == "volume_m3" else (1e-10 if key == "center_sketch_m" else 1e-6)
        return math.isclose(left, right, rel_tol=1e-7 if key == "volume_m3" else 1e-9, abs_tol=tolerance)
    return left == right


def _state(observation: dict[str, Any], *, selection: bool = False) -> dict[str, Any]:
    snapshot = observation.get("snapshot", {})
    fields = (*_SNAPSHOT_FIELDS, "selection_count") if selection else _SNAPSHOT_FIELDS
    value = {key: snapshot.get(key) for key in fields}
    for key in ("features", "dimensions", "planes", "document_identity"):
        if key in observation:
            value[key] = observation[key]
    if snapshot.get("active_sketch"):
        for key in ("active_sketch_geometry", "active_sketch_geometry_complete"):
            if key in observation:
                value[key] = observation[key]
    if selection and "selection" in observation:
        value["selection"] = observation["selection"]
    return value


def _identity(observation: dict[str, Any]) -> tuple[Any, ...]:
    snapshot = observation.get("snapshot", {})
    return (
        snapshot.get("document_title"), snapshot.get("document_path"),
        snapshot.get("document_type"), observation.get("document_identity"),
    )


def _snapshot_state(snapshot: ModelSnapshot | None) -> dict[str, Any] | None:
    return {key: getattr(snapshot, key) for key in _SNAPSHOT_FIELDS} if snapshot else None


class ContinuousTaskRunner(NativeStageRunnerMixin, TrialRunnerMixin):
    def __init__(
        self, registry: ToolRegistry, worker: ToolWorker, backend: TaskBackend,
        store: TaskStore,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        preflight: Callable[[ToolCall], tuple[bool, str]] | None = None,
        max_rounds: int = 48,
        task_id: str | None = None,
    ) -> None:
        if not 1 <= max_rounds <= 200:
            raise ValueError("max_rounds must be between 1 and 200")
        self.registry, self.worker, self.backend, self.store = registry, worker, backend, store
        self.on_event = on_event or (lambda _name, _payload: None)
        self.preflight = preflight or (lambda _call: (True, ""))
        self.max_rounds = max_rounds
        self._lock = threading.RLock()
        self._busy = threading.Lock()
        self._epoch = 0
        self._task_id = task_id
        self._cancelled = False
        self._busy_owner: int | None = None
        self._task: dict[str, Any] = {}
        self._failure_rearm_pending: tuple[str, ...] = ()
        self._init_native_stages()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if not self._task and self._cancelled:
                # A restore may be cancelled before its record is read. Report the
                # cancellation without writing over that untouched saved record.
                return {"id": self._task_id or "", "status": "stopped", "message": "任务已停止。"}
            return copy.deepcopy(self._task)

    def prepare(self, prompt: str, initial_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask for the first proposal without calling or connecting to SolidWorks."""
        if not prompt.strip():
            raise ValueError("请输入建模目标。")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("任务仍在运行。")
        try:
            self._busy_owner = threading.get_ident()
            with self._lock:
                self._epoch += 1
                epoch = self._epoch
                self._failure_rearm_pending = ()
                self._task = {
                    "id": self._task_id or uuid.uuid4().hex, "prompt": prompt.strip(), "status": "running",
                    "phase": "需求分析", "message": "正在明确目标和第一阶段。", "rounds": 0,
                    "goals": [], "assumptions": [], "questions": [], "pending_steps": [],
                    "completed_steps": [], "events": [], "verification": {}, "evidence": [],
                    "failure_attempts": [], "latest_observation": {}, "expected_observation": {},
                    "initial_context": scrub_agent_value(initial_context or {}),
                    "pending_decision": None, "feedback": [], "actions": 0,
                    "created_at": utc_now(), "updated_at": utc_now(),
                    "resume_pending": False, "dirty_failure": None, "bound_document": None,
                    "requirements": seed_requirements(prompt.strip(), "initial"),
                    "messages": [{"id": "initial", "role": "user", "content": prompt.strip(), "time": utc_now()}],
                    "pending_messages": [], "steering_revision": 0,
                    "diagnostic_history": [], "diagnostics": 0, "round_diagnostics": 0,
                    "no_progress": 0, "review_kind": "", "autonomy_attempts": {},
                    "created_features": [],
                    "created_circles": [], "goal_bindings": {}, "contract_audit": [], "contract_version": 2,
                    "diagnostic_no_progress": 0, "progress_fingerprints": [],
                    "failure_budget": new_budget(), "last_failure": {},
                }
                if self._cancelled:
                    self._task.update(status="stopped", message="任务已取消。")
            self._event("task_preparing", epoch)
            while self._alive(epoch) and self._task["status"] != "review":
                self._consume_steering(epoch)
                decision = self._decide(epoch)
                if decision is None:
                    if not self._alive(epoch) or self._task["status"] == "review":
                        break
                    continue
                if self._accept(decision, epoch):
                    if decision.action == "stop":
                        self._change(epoch, status="stopped", message=decision.reason)
                    elif decision.action == "review":
                        if self._handle_review(decision, epoch):
                            continue
                    else:
                        self._change(epoch, status="ready", pending_decision=decision.model_dump(mode="json"))
                    break
            return self.snapshot()
        finally:
            self._busy_owner = None
            self._busy.release()

    def run(self, single_stage: bool = False) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("任务仍在运行。")
        try:
            self._busy_owner = threading.get_ident()
            self._native_run_scope = True
            self._native_single_stage = single_stage
            with self._lock:
                epoch = self._epoch
                if not self._task or self._task["status"] in _TERMINAL:
                    return self.snapshot()
                if self._task["status"] == "review" and not self._task["resume_pending"]:
                    return self.snapshot()
            self._consume_steering(epoch)
            if self._task.get("active_trial"):
                self._review(epoch, "存在停止或重启前未完成的试验副本。请保留现场并新建任务，不能自动接管未确认试验。")
                return self.snapshot()
            observation = self._observe(epoch, recoverable=True)
            if observation is None:
                return self.snapshot()
            observation = self._reconnect_if_needed(observation, epoch) if observation.get("complete") is True else observation
            if observation is None:
                return self.snapshot()
            if self._task.get("gateway_context_reset") and observation["snapshot"].get("active_sketch"):
                self._review(epoch, "工作进程已重启，当前活动草图的会话绑定无法恢复。本版本仅支持完整特征检查点续作，请保留现场并重新开始任务。")
                return self.snapshot()
            if observation.get("complete") is not True:
                self._change(epoch, status="running")
            elif self._task["resume_pending"]:
                if not self._reconcile(observation, epoch):
                    return self.snapshot()
            elif not self._check_expected(observation, epoch):
                return self.snapshot()
            if observation.get("complete") is True:
                self._change(epoch, status="running", expected_observation=observation)
            while self._alive(epoch) and self._task["status"] == "running":
                self._consume_steering(epoch)
                raw_decision = self._task.get("pending_decision")
                decision: TaskDecision | None
                if raw_decision:
                    decision = TaskDecision.model_validate(raw_decision)
                    self._change(epoch, pending_decision=None)
                else:
                    decision = self._decide(epoch)
                    if decision is None:
                        continue
                    if not self._accept(decision, epoch):
                        continue
                if not self._alive(epoch):
                    break
                if decision.action == "review":
                    self._handle_review(decision, epoch)
                elif decision.action == "stop":
                    self._change(epoch, status="stopped", message=decision.reason)
                elif decision.action == "complete":
                    self._complete(epoch)
                elif decision.action == "inspect":
                    self._execute_inspections(decision, epoch)
                else:
                    if decision.trial is not None:
                        self._execute_trial(decision, epoch)
                    else:
                        self._execute_stage(epoch)
                    if single_stage and self._task["status"] == "running":
                        self._change(epoch, status="ready", message="本阶段已结束；继续时将基于实际结果重新决策。")
            return self.snapshot()
        except Exception as exc:
            if self._alive(self._epoch):
                self._review(self._epoch, f"任务运行异常，需要检查现场：{exc}")
            return self.snapshot()
        finally:
            self._native_run_scope = False
            self._native_decide_active = False
            self._busy_owner = None
            self._busy.release()

    def steer(self, message: str) -> dict[str, Any]:
        """Queue user input now; apply it only before a model call or CAD step."""
        message = str(message).strip()
        if not message or len(message) > 4000:
            raise ValueError("补充消息需要 1 至 4000 个字符。")
        with self._lock:
            if not self._task or self._cancelled or self._task.get("status") in {"stopped", "failed", "interrupted"}:
                raise ValueError("请先准备或恢复可继续的任务。")
            control = is_control_message(message)
            if self._task.get("status") == "completed" and not control:
                self._task.setdefault("verification_history", []).append(copy.deepcopy(self._task.get("verification", {})))
                busy = self._busy.locked()
                self._task.update(status="running" if busy else "ready", resume_pending=not busy,
                                  pending_decision=None, pending_steps=[])
            messages = self._task.setdefault("messages", [])
            if len(messages) >= 100:
                raise ValueError("本任务消息数量已达上限，请拆分任务。")
            item = {"id": f"u{len(messages) + 1}", "role": "user", "content": message, "time": utc_now(),
                    "purpose": "control" if control else "requirement"}
            messages.append(item)
            if control:
                self._change(self._epoch)
                self._event("control_message_recorded", self._epoch, message=message)
                return self.snapshot()
            self._task.setdefault("pending_messages", []).append(item)
            self._task["steering_revision"] = self._task.get("steering_revision", 0) + 1
            epoch = self._epoch
            self._change(epoch)
        self._event("steering_queued", epoch, message="已收到补充消息，将在当前安全边界后重新规划。")
        return self.snapshot()

    def _consume_steering(self, epoch: int) -> bool:
        with self._lock:
            if not self._alive(epoch) or not self._task.get("pending_messages"):
                return False
            requirements = self._task.setdefault("requirements", [])
            start = 1 + max((int(item["id"][1:]) for item in requirements
                             if item["id"].startswith("r") and item["id"][1:].isdigit()), default=0)
            for item in self._task["pending_messages"]:
                added = seed_requirements(item["content"], item["id"], start)
                requirements.extend(added)
                start += len(added)
            self._task["pending_messages"] = []
            self._change(epoch, pending_decision=None, pending_steps=[])
        self._feedback(epoch, "user_steering", "用户补充了需求；保留已完成证据，重新分解需求并规划后续。")
        return True

    def resume(self, note: str, *, as_requirement: bool = False) -> dict[str, Any]:
        if not note.strip() or len(note) > 4000:
            raise ValueError("请描述人工检查或操作结果。")
        with self._lock:
            if self._busy.locked():
                raise RuntimeError("任务仍在运行。")
            if self._task.get("status") != "review":
                raise ValueError("当前没有待人工检查的任务。")
            if self._task.get("rounds", 0) >= self.max_rounds or self._task.get("actions", 0) >= self.max_rounds * 12:
                raise ValueError("任务全局轮次或操作预算已用完，人工反馈不能重置；请保留证据并新建任务。")
            rearm = manual_rearm_candidates(self._task)
            if rearm and self._task.get("active_trial"):
                raise ValueError("存在未完成候选，不能通过人工备注重开错误预算；请保留现场并先处理候选边界。")
            messages = self._task.setdefault("messages", [])
            if len(messages) >= 100:
                raise ValueError("本任务消息数量已达上限，请拆分任务。")
            control = is_control_message(note)
            as_requirement = as_requirement and not control
            message = {"id": f"u{len(messages) + 1}", "role": "user", "content": note.strip(), "time": utc_now(),
                       "purpose": "control" if control else ("requirement" if as_requirement else "review_feedback")}
            messages.append(message)
            if as_requirement:
                self._task.setdefault("pending_messages", []).append(message)
                self._task["steering_revision"] = self._task.get("steering_revision", 0) + 1
            elif not control and self._task.get("review_kind") in {"clarification", "capability", "user_decision"}:
                requirements = self._task.setdefault("requirements", [])
                start = 1 + max((int(item["id"][1:]) for item in requirements
                                 if item["id"].startswith("r") and item["id"][1:].isdigit()), default=0)
                added = seed_requirements(note.strip(), message["id"], start)
                # A review note approves/discusses the current contract. New
                # dimensions must use steer() or explicit as_requirement=True.
                for item in added:
                    item["goal_ids"] = [goal["id"] for goal in self._task["goals"]]
                requirements.extend(added)
            epoch = self._epoch
            self._failure_rearm_pending = rearm
            self._feedback(epoch, "human_feedback", note.strip())
            self._change(epoch, resume_pending=True, pending_decision=None, pending_steps=[], status="ready",
                         message="已记录人工反馈；继续时将先重新观察并校验模型状态。")
        return self.snapshot()

    def restore(self, task_id: str) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("任务仍在运行。")
        try:
            with self._lock:
                if self._cancelled:
                    return self.snapshot()
                started_epoch = self._epoch
            value = self.store.load(task_id)
            if value is None:
                raise ValueError("找不到保存的任务。")
            # Never trust persisted proposals enough to execute after a process restart.
            for goal in value.get("goals", []):
                GoalCheck.model_validate(goal)
            with self._lock:
                if self._cancelled or self._epoch != started_epoch:
                    return self.snapshot()
                self._epoch += 1
                restored_epoch = self._epoch
                self._task = value
                self._failure_rearm_pending = ()  # Persisted JSON is never a resume authorization.
                ensure_budget(self._task)
                self._task.setdefault("requirements", seed_requirements(str(value.get("prompt", "")), "initial"))
                self._task.setdefault("messages", [{"id": "initial", "role": "user", "content": str(value.get("prompt", ""))}])
                for key, default in {"pending_messages": [], "steering_revision": 0,
                                     "diagnostic_history": [], "diagnostics": 0, "round_diagnostics": 0,
                                     "no_progress": 0, "review_kind": "", "autonomy_attempts": {}}.items():
                    self._task.setdefault(key, default)
                self._task.setdefault("created_features", [])
                self._task.setdefault("created_circles", [])
                self._task.setdefault("goal_bindings", {})
                self._task.setdefault("contract_audit", [])
                self._task.setdefault("diagnostic_no_progress", 0)
                self._task.setdefault("progress_fingerprints", [])
                migrations = migrate_control_requirements(self._task)
                self._task["contract_audit"].extend({**item, "kind": "control_source_migration", "time": utc_now()}
                                                     for item in migrations)
                if self._task.get("contract_version", 1) < 2:
                    # Failure-policy migration has already separated known old
                    # events and retained unclassified history conservatively.
                    self._task.update(contract_version=2)
                    self._task["contract_audit"].append({"kind": "strategy_budget_migration", "time": utc_now()})
                observations = [self._task.get("latest_observation", {}), self._task.get("expected_observation", {})]
                observations.extend(event.get("observation", {}) for event in self._task.get("events", [])
                                    if event.get("name") == "observation_completed")
                for raw in self._task.get("goals", []):
                    goal = GoalCheck.model_validate(raw)
                    if goal.metric in {"dimension_mm", "feature_present"} and goal.binding == "fixed":
                        verified = any(observed_target(goal, obs) for obs in observations)
                        self._task["goal_bindings"].setdefault(goal.id, {
                            "target": goal.target, "metric": goal.metric, "verified": verified,
                            "legacy_unverified": not verified, "rebindings": 0,
                        })
                self._task.update(
                    status="review", resume_pending=False, pending_decision=None, pending_steps=[],
                    restored=True,
                    message="任务已恢复。请检查当前文档并说明现场状态，重新观察验证后才能继续。",
                )
            self._event("task_restored", restored_epoch)
            return self.snapshot()
        finally:
            self._busy.release()

    def stop(self) -> None:
        with self._lock:
            self._epoch += 1
            self._cancelled = True
            self._failure_rearm_pending = ()
            epoch = self._epoch
            if self._task and self._task.get("status") != "completed":
                self._task.update(status="stopped", message="已停止所有后续动作；在途操作需检查现场。",
                                  pending_decision=None, pending_steps=[], resume_pending=False)
                self._event("task_stopped", epoch)
        try:
            interrupt = getattr(self.worker, "interrupt", None)
            if callable(interrupt):
                interrupt()
        finally:
            # A Pi process close may wait seconds; cut off CAD dispatch first.
            self.backend.close()

    def _alive(self, epoch: int) -> bool:
        with self._lock:
            return bool(self._task) and not self._cancelled and epoch == self._epoch and self._task.get("status") not in _TERMINAL

    def _change(self, epoch: int, **updates: Any) -> bool:
        with self._lock:
            if epoch != self._epoch:
                return False
            self._task.update(updates)
            self._task["updated_at"] = utc_now()
            self.store.save(self._task)
        return True

    def _event(self, name: str, epoch: int, **payload: Any) -> None:
        with self._lock:
            if epoch != self._epoch:
                return
            event = {"name": name, "time": utc_now(), **scrub_agent_value(payload)}
            self._task["events"].append(event)
            self._change(epoch)
        self.on_event(name, copy.deepcopy(event))

    def _review(self, epoch: int, message: str, kind: str = "safety", *, failure_category: str = "safety") -> None:
        with self._lock:
            if not self._alive(epoch):
                return
            assessment = record_failure(self._task, failure_category, force_review=True)
            self._change(epoch, status="review", message=message, resume_pending=False,
                         pending_decision=None, pending_steps=[], review_kind=kind)
        self._event("task_review", epoch, message=message, **assessment)

    def _feedback(self, epoch: int, kind: str, message: str, **details: Any) -> None:
        with self._lock:
            if epoch != self._epoch or self._cancelled:
                return
            category = classify_failure(kind, details) if self._task.get("status") not in _TERMINAL else None
            prior_failure = self._task.get("last_failure", {})
            assessment = (record_failure(self._task, category, force_review=self._task.get("status") == "review")
                          if category else {})
            if (self._task.get("status") == "review" and prior_failure.get("requires_review")
                    and prior_failure.get("failure_category") != category):
                self._task["last_failure"] = prior_failure
            self._task["feedback"].append({"kind": kind, "message": message, "time": utc_now(),
                                           "round": self._task.get("rounds", 0), **details, **assessment})
            # Late informational feedback belongs in the record, but must not
            # replace the reason the task is waiting for review or has ended.
            if self._task.get("status") in _TERMINAL | {"review"}:
                self._change(epoch)
            else:
                self._change(epoch, message=message)
        self._event("task_feedback", epoch, kind=kind, message=message, **assessment)
        if (self._alive(epoch) and self._task.get("status") != "review"
                and category and assessment["requires_review"]):
            self._review(epoch, f"{FAILURE_LABELS[category]}已达到本阶段处理边界"
                         f"（{assessment['attempts']}/{assessment['limit']}），自动推进已暂停。"
                         f"\n最近阻碍：{message[:500]}",
                         "safety" if category in {"safety", "unknown"} else "capability", failure_category=category)

    def _progress(self, epoch: int, kind: str, evidence: Any) -> None:
        if kind == "verified_geometry" and self._task.get("active_trial"):
            kind = "tentative_geometry"  # Never hide the later formal-commit fingerprint.
        fingerprint = _signature({"kind": kind, "evidence": _evidence_value(evidence)})
        with self._lock:
            if not self._alive(epoch):
                return
            seen = self._task.setdefault("progress_fingerprints", [])
            if fingerprint not in seen:
                seen.append(fingerprint)
                if kind == "verified_geometry" and not self._task.get("active_trial"):
                    reset_verified_window(self._task)
                    self._change(epoch, diagnostic_no_progress=0, autonomy_attempts={})
                else:
                    self._change(epoch)

    def _context(self) -> dict[str, Any]:
        task = self.snapshot()
        completed = task["completed_steps"]
        initialized = any(item["tool"] == "document.new_part" for item in completed)
        observed = task.get("latest_observation", {}).get("snapshot", {})
        startup = {
            "phase": "modeling" if initialized else "initialization_required",
            "connected": observed.get("connected"), "document_type": observed.get("document_type"),
            "suggested_initial_steps": ([] if initialized else
                                        (["session.ensure"] if not any(item["tool"] == "session.ensure" for item in completed) else [])
                                        + ["document.new_part"]),
            "instruction": ("保留成功前缀，基于已读回证据提交下一阶段。" if initialized else
                            "本任务尚无已创建文档。连接/无文档状态不必反复调查；用 act 的 steps 提议初始化，"
                            "先定义最终目标及需求映射，未知真实名称标 pending。初始化成功后再读实际基准面。"),
            "event_bridge_unavailable_is_not_cad_failure": True,
        }
        compact_evidence = [{
            "call": item["call"], "ok": item["ok"], "message": item["message"][:1000],
            "data": item["data"], "after": item["after"],
        } for item in task["evidence"][-8:]]
        failures = [{"call": item["call"], "fingerprint": item["fingerprint"],
                     "geometry_evidence": failure_geometry_summary(item),
                     "result": {key: item["result"].get(key)
                                for key in ("ok", "message", "error_code", "before", "after")}}
                    for item in task["failure_attempts"][-8:]]
        return scrub_agent_value({
            "task_id": task["id"], "task": task["prompt"], "goals": task["goals"],
            "trial_workspace": self._trial_capabilities(),
            "agent_stage_execution": self._native_stage_capabilities(),
            "trial_history": [{key: item.get(key) for key in ("program", "trial", "status", "checks", "failure", "finish_receipt")}
                              for item in task.get("trial_history", [])[-6:]],
            "locked_goals": task["goals"], "phase": task["phase"],
            "startup": startup,
            "requirements": task.get("requirements", []), "messages": task.get("messages", [])[-32:],
            "contract_issues": contract_issues(task.get("requirements", []), task["goals"]),
            "diagnostic_history": task.get("diagnostic_history", [])[-8:],
            "goal_bindings": task.get("goal_bindings", {}), "contract_audit": task.get("contract_audit", [])[-16:],
            "created_circles": task.get("created_circles", []),
            "review_kind": task.get("review_kind", ""),
            "failure_policy": {"last_failure": task.get("last_failure", {}),
                               "budgets": remaining_failures(task),
                               "counting_unit": "Each category at most once per decision round; global action/round limits still apply.",
                               "manual_resume_rule": "One extra window per task only after explicit user resume and independent scene reconciliation; never for adapter/safety/unknown.",
                               "reset_rule": "Only committed candidate or independently verified non-tentative geometry; never reads or contract edits.",
                               "legacy_unclassified": task["failure_budget"].get("legacy_unclassified", 0)},
            "assumptions": task["assumptions"], "completed_steps": completed[-64:],
            "completed_summary": {"count": len(completed), "by_tool": {
                name: sum(item["tool"] == name for item in completed)
                for name in sorted({item["tool"] for item in completed})}},
            "completed_evidence": compact_evidence, "failure_attempts": failures,
            "latest_observation": task["latest_observation"], "feedback": task["feedback"][-20:],
            "verification": task["verification"], "initial_context": task["initial_context"],
            "execution_constraints": ([
                "COM 工作进程已重新连接；上一特征端面引用与活动草图绑定已失效。",
                "只能从完整特征检查点继续；新草图须先使用稳定基准面，建立新特征后才重新拥有端面引用。",
            ] if task.get("gateway_context_reset") else []),
            "remaining_budget": {"rounds": max(0, self.max_rounds - task["rounds"]),
                                 "actions": max(0, self.max_rounds * 12 - task["actions"]),
                                 "diagnostics": max(0, min(96, self.max_rounds * 8) - task.get("diagnostics", 0)),
                                 "round_investigations": max(0, 8 - task.get("round_diagnostics", 0)),
                                 "unchanged_attempts": remaining_failures(task)["planning"]["remaining"],
                                 "failure_categories": remaining_failures(task),
                                 "unchanged_investigations": max(0, 6 - task.get("diagnostic_no_progress", 0))},
        })

    def _decide(self, epoch: int) -> TaskDecision | None:
        if not self._alive(epoch):
            return None
        exhausted = exhausted_failure(self._task)
        if exhausted:
            self._review(epoch, f"{FAILURE_LABELS[exhausted]}处理预算已用完；请检查现场或拆分为新任务，未继续请求模型。",
                         "capability", failure_category=exhausted)
            return None
        if self._task["rounds"] >= self.max_rounds:
            self._review(epoch, "已达到本次任务决策轮次上限，请检查目标和失败原因。")
            return None
        revision = self._task.get("steering_revision", 0)
        self._change(epoch, rounds=self._task["rounds"] + 1, round_diagnostics=0, round_investigation_closed=False,
                     message="Agent 正在根据实际反馈判断下一步。")
        self._event("agent_thinking", epoch, round=self._task["rounds"])
        if not self._alive(epoch):
            return None
        try:
            self._native_decision_epoch = epoch
            self._native_decision_revision = revision
            self._native_stage_attempts = 0
            self._native_stage_executions = 0
            self._native_decide_active = self._native_run_scope
            decision = self.backend.decide(self._context())
            if not self._alive(epoch) or self._task["status"] in {"review", "ready"}:
                return None
            if revision != self._task.get("steering_revision", 0):
                self._consume_steering(epoch)
                return None
            decision = TaskDecision.model_validate(decision)
            if self._native_stage_attempts and decision.action == "act":
                self._change(epoch, pending_decision=None, pending_steps=[])
                self._feedback(epoch, "native_stage_handoff", "本轮已经通过原生工具提交阶段，最终 act 不会再次执行；正在基于最新检查点继续决策。")
                return None
            return decision
        except Exception as exc:
            if self._alive(epoch):
                if getattr(exc, "fatal", False) or not (
                    isinstance(exc, ValueError) or getattr(exc, "retryable_decision", False)
                ):
                    self._review(epoch, f"Agent 当前不可用：{exc}", failure_category="environment")
                else:
                    self._feedback(epoch, "decision_rejected", f"决策无效，请修正：{exc}")
            return None
        finally:
            self._native_decide_active = False

    def _accept(self, decision: TaskDecision, epoch: int) -> bool:
        try:
            bindings = copy.deepcopy(self._task.get("goal_bindings", {}))
            audit: list[dict[str, Any]] = []
            previous_issues = contract_issues(self._task.get("requirements", []), self._task["goals"])
            goals = refine_goals(self._task["goals"], decision.goals,
                                 self._goal_binding_observation(self._task.get("latest_observation", {})),
                                 bindings=bindings, audit=audit)
            if decision.action in {"act", "complete"} and not goals:
                raise ValueError("开始建模前必须定义可测量的最终验收目标。")
            requirements = merge_requirements(
                self._task.get("requirements", []), decision.requirements, goals,
                require_complete=decision.action in {"act", "complete"},
            )
            calls = [ToolCall.from_mapping(step.model_dump(mode="python")) for step in decision.steps]
            if (self._trial_capabilities()["enabled"] and decision.action == "act" and decision.trial is None
                    and any(call.tool in _MODEL_TOOLS - {"session.ensure", "document.new_part"} for call in calls)):
                raise ValueError("建模修改必须通过带 trial.hypothesis / trial.checks 的完整副本候选阶段；先单独初始化新零件。")
            if decision.trial is not None:
                if not self._trial_capabilities()["enabled"]:
                    raise ValueError("当前工作进程未启用副本试验能力。")
                if any(call.tool in {"session.ensure", "document.new_part", "sketch.begin_face", "sketch.set_circle_radius"}
                       for call in calls):
                    raise ValueError("试验不得初始化/切换文档或继承其他文档的端面/可编辑实体引用。请使用稳定基准面建立完整候选阶段。")
                if not any(call.tool in {"feature.extrude", "feature.revolve"} for call in calls):
                    raise ValueError("副本候选必须包含完整实体特征建模，不能仅靠复制或只读调查作为进展。")
            for call in calls:
                if call.tool not in _READ_TOOLS | _MODEL_TOOLS:
                    raise ValueError(f"持续建模不授权工具：{call.tool}")
                if decision.action == "inspect" and call.tool not in _READ_TOOLS:
                    raise ValueError("inspect 只能请求只读观察。")
                if call.tool == "sketch.begin_face" and self._task.get("gateway_context_reset"):
                    raise ValueError("工作进程重连后上一特征端面引用已失效，请使用稳定基准面或请求人工检查。")
                if self.registry.write_scope(call.tool) not in {"none", "document", "application"}:
                    raise ValueError("持续建模不得写入本地文件。")
            prefix = [ToolCall.from_mapping(item) for item in self._task["completed_steps"]]
            normalized = self.registry.validate_plan(prefix + calls)[len(prefix):]
            self._change(epoch, goals=goals, requirements=requirements, goal_bindings=bindings,
                         contract_audit=self._task.get("contract_audit", []) + [{**item, "time": utc_now()} for item in audit],
                         phase=decision.phase or self._task["phase"],
                         assumptions=list(dict.fromkeys(self._task["assumptions"] + decision.assumptions)),
                         questions=decision.questions, message=decision.reason,
                         pending_steps=[call.to_dict() for call in normalized])
            if audit or len(contract_issues(requirements, goals)) < len(previous_issues):
                self._progress(epoch, "contract", {"requirements": requirements, "bindings": bindings})
            self._event("agent_decision", epoch, decision=decision.model_dump(mode="json"))
            return self._alive(epoch)
        except Exception as exc:
            self._feedback(epoch, "policy_rejected", f"本地校验拒绝本阶段，请修正：{exc}")
            self._diagnose(epoch, "candidate")
            return False

    def investigate(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Read-only native-agent bridge; it cannot commit proposals or CAD writes."""
        return self._investigate(tool, arguments, automatic=False)

    def _investigate(self, tool: str, arguments: dict[str, Any], *, automatic: bool) -> dict[str, Any]:
        epoch = self._epoch
        rejected = {"ok": False, "tool": tool, "data": {}}
        if not self._alive(epoch):
            return {**rejected, "error_code": "TaskStopped", "message": "任务已停止。"}
        if self._task.get("status") == "review":
            return {**rejected, "error_code": "ReviewRequired", "message": "任务已进入人工检查状态，请先处理检查要求。"}
        if self._busy.locked() and self._busy_owner != threading.get_ident():
            return {**rejected, "error_code": "TaskBusy", "message": "调查必须在任务执行线程的安全边界中进行。"}
        if not automatic and self._task.get("round_investigation_closed"):
            return {**rejected, "error_code": "InvestigationDecisionRequired", "decision_only": True,
                    "message": "本轮调查已结束；请保留成功前缀和锁定目标，直接提交下一阶段 JSON 决策。"}
        if self._task.get("diagnostics", 0) >= min(96, self.max_rounds * 8):
            self._review(epoch, "本任务调查预算已用完，请根据已收集证据决定是否拆分任务。", "capability")
            return {**rejected, "error_code": "DiagnosticBudgetExceeded", "message": "任务调查预算已用完。"}
        if self._task.get("round_diagnostics", 0) >= 8:
            return {**rejected, "error_code": "RoundDiagnosticLimit", "decision_only": True,
                    "message": "本轮最多调查八次，请先根据已有证据作出决策。"}
        self._change(epoch, diagnostics=self._task.get("diagnostics", 0) + 1,
                     round_diagnostics=self._task.get("round_diagnostics", 0) + 1)
        try:
            if tool not in _READ_TOOLS:
                raise ValueError("调查接口只允许明确白名单内的只读工具。")
            call = self.registry.validate(ToolCall(tool, arguments))
            if self.registry.write_scope(tool) != "none":
                raise ValueError("调查接口不得调用写操作。")
            allowed, reason = self.preflight(call)
            if not self._alive(epoch):
                return {**rejected, "error_code": "TaskStopped", "message": "任务已停止。"}
            if not allowed:
                raise PermissionError(reason or "当前不允许调查。")
            self._event("investigation_started", epoch, tool=tool, arguments=call.arguments)
            if not self._alive(epoch):
                return {**rejected, "error_code": "TaskStopped", "message": "任务已停止。"}
            if tool == "diagnostics.recent":
                from .diagnostics import inspect_recent_failures

                data = inspect_recent_failures(self.snapshot(), self.registry, limit=call.arguments.get("limit", 3))
                result = StepResult(True, call, "已读取本任务近期诊断。", data=data)
            else:
                result = self.worker.call(call, 60)
            if not self._alive(epoch):
                return {**rejected, "error_code": "TaskStopped", "message": "任务已停止，已丢弃晚到调查结果。"}
            output: dict[str, Any] = {"ok": result.ok, "tool": tool, "arguments": call.arguments,
                      "message": result.message, "error_code": result.error_code, "data": result.data,
                      "duration_ms": result.duration_ms}
        except Exception as exc:
            output = {**rejected, "arguments": arguments, "error_code": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, PermissionError):
                self._review(epoch, str(exc), "safety")
        if not self._alive(epoch):
            return {**rejected, "error_code": "TaskStopped", "message": "任务已停止。"}
        with self._lock:
            if epoch != self._epoch:
                return {**rejected, "error_code": "TaskStopped", "message": "任务已停止，已丢弃晚到调查结果。"}
            fingerprint = _signature(_evidence_value({key: value for key, value in output.items() if key != "message"}))
            history = self._task.setdefault("diagnostic_history", [])
            novel = not any(item.get("fingerprint") == fingerprint for item in history)
            history.append({**copy.deepcopy(output), "fingerprint": fingerprint, "time": utc_now(),
                            "origin": "host" if automatic else "agent"})
            if tool == "model.observe" and output.get("ok") and isinstance(output.get("data", {}).get("snapshot"), dict):
                observation = output["data"]
                expected = self._task.get("expected_observation")
                if not expected or _identity(expected) == _identity(observation):
                    # Feed native reads back into planning/diagnostics, never
                    # promote them to an execution checkpoint or verify a write.
                    self._task["latest_observation"] = copy.deepcopy(observation)
            if not automatic:
                self._task["diagnostic_no_progress"] = (0 if novel and output["ok"]
                                                       and output["data"].get("complete") is True
                                                       else self._task.get("diagnostic_no_progress", 0) + 1)
                output["evidence_novel"] = bool(novel and output["ok"] and output["data"].get("complete") is True)
                if self._task["diagnostic_no_progress"] >= 6:
                    self._task["round_investigation_closed"] = True
                    output.update(decision_only=True, error_code="InvestigationDecisionRequired",
                                  message="重复调查未带来新证据，本轮转为提交决策；保留成功前缀和锁定目标，"
                                          "请返回可执行的下一阶段 JSON，不需要人工确认。")
            self._change(epoch)
        self._event("investigation_completed", epoch, result=output)
        if not automatic and (not novel or not output["ok"]):
            self._feedback(epoch, "investigation_no_progress", "本次调查未提供新的可用证据，请改变调查问题或选择替代路线。")
        if output.get("decision_only") and self._alive(epoch) and self._task["status"] != "review":
            self._feedback(epoch, "investigation_decision_required", output["message"])
        return copy.deepcopy(output)

    def _diagnose(self, epoch: int, category: str) -> None:
        if not self._alive(epoch) or self._task["status"] == "review":
            return
        available = {item["name"] for item in self.registry.describe()}
        tools = ["diagnostics.recent", "model.references" if category == "selection" else "session.capabilities"]
        for tool in tools:
            if tool in available and self._alive(epoch) and self._task["status"] != "review":
                arguments = {"limit": 3} if tool == "diagnostics.recent" else ({"limit": 20} if tool == "model.references" else {})
                self._investigate(tool, arguments, automatic=True)

    def _execute_inspections(self, decision: TaskDecision, epoch: int) -> None:
        steps = decision.steps or []
        results = []
        if not steps:
            results.append(self.investigate("model.observe", {}))
        for step in steps:
            if not self._alive(epoch) or self._task["status"] == "review" or self._consume_steering(epoch):
                return
            results.append(self.investigate(step.tool, step.arguments))
            if results[-1].get("decision_only"):
                break
        if not self._alive(epoch) or self._task["status"] == "review":
            return
        self._change(epoch, pending_steps=[])
        if results and not any(item.get("evidence_novel") for item in results):
            # This is an actual inspect proposal with no effect, not a native
            # read while the Agent is still forming its next actionable proposal.
            self._feedback(epoch, "strategy_no_progress", "本次只读调查提案未取得新进展；请根据已有证据提交建模或纠正决策。")
        else:
            self._feedback(epoch, "investigation_feedback", "已执行请求的只读调查；请使用 diagnostic_history 中的具体结果继续规划。")

    def _handle_review(self, decision: TaskDecision, epoch: int) -> bool:
        kind = decision.review_kind
        if kind in {"safety", "user_decision"} or decision.review_critical:
            self._review(epoch, decision.reason, kind)
            return False
        attempts = self._task.setdefault("autonomy_attempts", {})
        attempts[kind] = attempts.get(kind, 0) + 1
        if attempts[kind] > 2:
            self._review(epoch, decision.reason, kind)
            return False
        self._change(epoch, review_kind=kind)
        if kind == "capability":
            self._diagnose(epoch, "capability")
            message = "先依据已查询能力和工具结果寻找替代路线；若仍不能覆盖要求，再说明具体缺失能力。"
        else:
            message = "非关键缺省请先提出明确、可逆的合理假设并继续方案；只有会实质改变目标的选择才请求用户决定。"
        self._feedback(epoch, "autonomy_retry", message, previous_request=decision.reason, questions=decision.questions)
        return self._task["status"] != "review"

    def _observe(self, epoch: int, *, recoverable: bool = False, include_configuration: bool = False) -> dict[str, Any] | None:
        if not self._alive(epoch):
            return None
        self._event("observation_started", epoch)
        if not self._alive(epoch):
            return None
        try:
            arguments = {"include_configuration": True} if include_configuration or self._task.get("active_trial") else {}
            result = self.worker.call(ToolCall("model.observe", arguments), 60)
            if not self._alive(epoch):
                return None
            observation = copy.deepcopy(result.data)
            if not result.ok or observation.get("complete") is not True:
                if recoverable and self._identity_known(observation):
                    self._change(epoch, latest_observation=observation, pending_decision=None, pending_steps=[])
                    self._feedback(epoch, "observation_incomplete", "当前文档身份已确认，但部分观察信息不完整；请调查相关状态后调整计划。",
                                   observation=observation, error=result.message)
                    self._diagnose(epoch, "observation")
                    return observation if self._task["status"] != "review" else None
                self._review(epoch, f"无法完整读取模型状态，暂停建模：{result.message}",
                             failure_category="observation" if self._identity_known(observation) else "unknown")
                return None
            snapshot = observation.get("snapshot")
            if not isinstance(snapshot, dict) or any(key not in snapshot for key in _SNAPSHOT_FIELDS):
                self._review(epoch, "观察缺少必要的文档或几何状态，暂停建模。", failure_category="unknown")
                return None
            strings = ("document_title", "document_path", "document_type", "active_sketch")
            counts = ("sketch_entity_count", "feature_count", "body_count", "selection_count")
            if (not isinstance(snapshot.get("connected"), bool)
                or any(not isinstance(snapshot.get(key), str) for key in strings)
                or any(not self._number(snapshot.get(key), 0, "ge", 0) for key in counts)
                or not self._number(snapshot.get("volume_m3"), 0, "ge", 0)):
                if recoverable and self._identity_known(observation):
                    observation["complete"] = False
                    self._change(epoch, latest_observation=observation, pending_decision=None, pending_steps=[])
                    self._feedback(epoch, "observation_incomplete", "文档身份已确认，但几何字段尚未可靠读取；先调查，不允许写入。")
                    self._diagnose(epoch, "observation")
                    return observation if self._task["status"] != "review" else None
                self._review(epoch, "观察包含未知的文档或几何字段，暂停建模。", failure_category="unknown")
                return None
            _signature(_state(observation))
            self._change(epoch, latest_observation=observation)
            bindings = copy.deepcopy(self._task.get("goal_bindings", {}))
            audit: list[dict[str, Any]] = []
            goals = refine_goals(self._task.get("goals", []), [], self._goal_binding_observation(observation),
                                 bindings=bindings, audit=audit)
            if audit:
                self._change(epoch, goals=goals, goal_bindings=bindings,
                             contract_audit=self._task.get("contract_audit", []) + [{**item, "time": utc_now()} for item in audit])
                self._progress(epoch, "binding", bindings)
            self._event("observation_completed", epoch, observation=observation)
            return observation if self._alive(epoch) else None
        except Exception as exc:
            if self._alive(epoch):
                self._review(epoch, f"模型观察失败：{exc}", failure_category="observation")
            return None

    def _goal_binding_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        """A pre-existing document is diagnostic input, not the new task's named geometry."""
        if not any(item.get("tool") == "document.new_part" for item in self._task.get("completed_steps", [])):
            return {}
        expected = self._task.get("expected_observation")
        if expected and _identity(expected) != _identity(observation):
            # A restored helper must first pass _reconcile, which replaces the
            # expected identity without dropping goals or legacy binding proof.
            return {}
        return observation

    def _identity_known(self, observation: dict[str, Any]) -> bool:
        snapshot = observation.get("snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("connected"), bool):
            return False
        if any(not isinstance(snapshot.get(key), str) for key in ("document_title", "document_path", "document_type")):
            return False
        expected = self._task.get("expected_observation")
        if expected and _identity(expected) != _identity(observation):
            return False
        return snapshot["document_type"] == "none" or bool(snapshot["document_title"])

    def _check_expected(self, observation: dict[str, Any], epoch: int) -> bool:
        expected = self._task.get("expected_observation")
        if (self._task.get("active_trial") or {}).get("status") == "running":
            changed_stamp = bool(expected and "model_update_stamp" in expected
                                 and expected["model_update_stamp"] != observation.get("model_update_stamp"))
            if changed_stamp or not expected or any(not expected.get(name) or expected.get(name) != observation.get(name)
                                                   for name in ("configuration", "configuration_identity")):
                self._review(epoch, "试验期间活动配置改变或身份未知，未继续执行候选程序。")
                return False
        if expected and not _equivalent(_state(expected), _state(observation)):
            self._review(epoch, "当前文档或模型状态已在任务外改变，请检查后说明操作结果。")
            return False
        return True

    def _reconnect_if_needed(self, observation: dict[str, Any], epoch: int) -> dict[str, Any] | None:
        """Reattach a replaced helper; never replay document creation or geometry."""
        expected = self._task.get("expected_observation", {})
        was_connected = expected.get("snapshot", {}).get("connected") is True
        can_reconnect = any(step.get("tool") in {"session.ensure", "document.new_part"}
                            for step in self._task.get("completed_steps", []))
        if observation["snapshot"].get("connected") is not False or not was_connected or not can_reconnect:
            return observation
        call = ToolCall("session.ensure", {}, "重新连接已建立的任务会话")
        if not self._alive(epoch):
            return None
        if self._task["actions"] >= self.max_rounds * 12:
            self._review(epoch, "已达到任务操作数上限，无法继续重新连接。")
            return None
        try:
            allowed, reason = self.preflight(call)
            if not self._alive(epoch):
                return None
            if not allowed:
                self._review(epoch, reason or "当前不允许重新连接 SolidWorks。")
                return None
            self._change(epoch, actions=self._task["actions"] + 1)
            self._event("connection_refresh_started", epoch, message="正在重新连接 SolidWorks，随后核对原任务文档。")
            if not self._alive(epoch):
                return None
            result = self.worker.call(call, 120)
            if not self._alive(epoch):
                return None
            verifier = self.registry.postcondition(call.tool)
            if not result.ok or verifier is None or not verifier(call, result.data, result.before, result.after)[0]:
                self._review(epoch, "无法恢复 SolidWorks 连接：" + result.message)
                return None
            self._change(epoch, connection_refreshed=True, gateway_context_reset=True)
            self._feedback(epoch, "connection_refreshed", "已重新连接 SolidWorks；未重放任何建模步骤，正在核对原模型。")
            return self._observe(epoch)
        except Exception as exc:
            if self._alive(epoch):
                self._review(epoch, f"重新连接 SolidWorks 失败：{exc}")
            return None

    def _reconcile(self, observation: dict[str, Any], epoch: int) -> bool:
        expected = self._task.get("expected_observation")
        restored = bool(self._task.get("restored") or self._task.get("connection_refreshed"))
        if (restored and expected and expected.get("snapshot", {}).get("active_sketch")
                and "active_sketch_geometry" not in expected and "active_sketch_geometry" in observation):
            self._review(epoch, "旧任务缺少活动草图的可验证创建引用与完整几何上下文，不能安全自动接续。请保留未保存模型，人工处理原现场或新建任务。")
            return False

        def comparable(value: dict[str, Any]) -> dict[str, Any]:
            state = _state(value)
            if restored:
                state.pop("document_identity", None)
            return state

        expected_id = _identity(expected) if expected else ()
        current_id = _identity(observation)
        if expected and (expected_id[:3] != current_id[:3] or
                         (not restored and expected_id != current_id)):
            self._review(epoch, "当前活动文档与任务文档不同，请切回原文档后继续。")
            return False
        dirty = self._task.get("dirty_failure")
        if dirty:
            baseline = dirty.get("before_observation")
            if not baseline or not _equivalent(comparable(baseline), comparable(observation)):
                self._review(epoch, "失败步骤可能已部分改变模型。请撤销到失败前状态，再提交人工反馈。")
                return False
            self._feedback(epoch, "state_reconciled", "新观察已确认模型回到失败前状态；未将失败操作标记为完成。")
        elif expected and not _equivalent(comparable(expected), comparable(observation)):
            self._review(epoch, "人工修改改变了已完成特征，暂不能证明原计划依赖仍有效；请恢复原状态或新建任务。")
            return False
        with self._lock:
            if not self._alive(epoch):
                return False
            if self._failure_rearm_pending:
                categories = self._failure_rearm_pending
                self._failure_rearm_pending = ()
                grant_manual_window(self._task, categories)
                self._event("failure_budget_rearmed", epoch, categories=list(categories),
                            message="人工核对后的现场已独立验证，已开启一次有限纠正窗口；累计失败与全局预算保持不变。")
                if not self._alive(epoch):
                    return False
        self._change(epoch, resume_pending=False, dirty_failure=None, restored=False, connection_refreshed=False,
                     expected_observation=observation,
                     pending_decision=None, pending_steps=[], status="running")
        self._event("task_resumed", epoch)
        return True

    def _execute_stage(self, epoch: int) -> None:
        calls = [ToolCall.from_mapping(item) for item in self._task["pending_steps"]]
        progress_before = len(self._task.get("progress_fingerprints", []))
        self._event("stage_started", epoch, count=len(calls))
        for index, call in enumerate(calls):
            if not self._alive(epoch) or self._task["status"] != "running":
                return
            if self._task.get("active_trial") and self._task.get("pending_messages"):
                return  # Restore first; consume new requirements on the verified baseline.
            if self._consume_steering(epoch):
                return
            if self._task["actions"] >= self.max_rounds * 12:
                self._review(epoch, "达到本次任务操作数上限。")
                return
            observation = self._observe(epoch, recoverable=True)
            if observation is None or observation.get("complete") is not True:
                return
            if not self._check_expected(observation, epoch):
                return
            if call.tool == "sketch.set_circle_radius" and not self._authorize_circle_edit(call, observation, epoch):
                return
            failed_key = _signature({"tool": call.tool, "arguments": call.arguments,
                                     "state": _state(observation, selection=True)})
            if any(
                item.get("fingerprint") == failed_key or (
                    item.get("call", {}).get("tool") == call.tool
                    and item.get("call", {}).get("arguments") == call.arguments
                    and _equivalent(item.get("state"), _state(observation, selection=True))
                ) for item in self._task["failure_attempts"]
            ):
                self._change(epoch, pending_steps=[])
                self._feedback(epoch, "repeat_blocked", "同一模型状态下该操作已经失败；请先改变参数或修正操作前提。")
                self._diagnose(epoch, "selection" if call.tool.startswith(("selection.", "sketch.begin")) else "candidate")
                return
            try:
                allowed, message = self.preflight(call)
            except Exception as exc:
                allowed, message = False, str(exc)
            if not self._alive(epoch):
                return
            if not allowed:
                # A free-text denial is not a typed recovery authorization.
                self._review(epoch, message or "当前上下文不允许执行操作。")
                return
            self._change(epoch, actions=self._task["actions"] + 1)
            self._event("step_started", epoch, call=call.to_dict())
            if not self._alive(epoch):
                return
            try:
                checked_call = getattr(self.worker, "call_checked", None)
                if call.tool not in {"document.new_part", "session.ensure"} and callable(checked_call):
                    result = checked_call(call, observation, 120)
                else:
                    result = self.worker.call(call, 120)
            except Exception as exc:
                result = StepResult(False, call, str(exc), error_code=type(exc).__name__)
            if not self._alive(epoch):
                return
            # Even a worker's success flag must satisfy the production verifier.
            if result.ok:
                verifier = self.registry.postcondition(call.tool)
                if verifier:
                    try:
                        result.ok, message = verifier(call, result.data, result.before, result.after)
                    except Exception as exc:
                        result.ok, message = False, f"后验检查异常：{exc}"
                    if not result.ok:
                        result.error_code, result.message = "PostconditionFailed", message
            after = self._observe(epoch)
            if after is None:
                if self._alive(epoch):
                    self._change(epoch, dirty_failure={"before_observation": observation,
                                                      "result": result.to_dict()})
                return
            # Detect context races even if the native operation itself returned success.
            if call.tool not in {"document.new_part", "session.ensure"}:
                if result.before is not None and not _equivalent(_snapshot_state(result.before), {
                    key: observation["snapshot"].get(key) for key in _SNAPSHOT_FIELDS
                }):
                    result.ok, result.error_code = False, "StateChanged"
                    result.message = "操作开始时的模型状态与刚才观察不一致，需要人工确认。"
                if _identity(observation) != _identity(after):
                    result.ok, result.error_code = False, "DocumentChanged"
                    result.message = "操作期间活动文档改变，结果需人工确认。"
            if result.ok and result.after is not None:
                if not _equivalent(_snapshot_state(result.after), {
                    key: after["snapshot"].get(key) for key in _SNAPSHOT_FIELDS
                }):
                    result.ok, result.error_code = False, "StateChanged"
                    result.message = "工具返回后模型状态再次改变，需要人工检查。"
            if not result.ok:
                with self._lock:
                    if epoch != self._epoch:
                        return
                    self._task["failure_attempts"].append({
                        "fingerprint": failed_key, "call": call.to_dict(), "result": result.to_dict(),
                        "observation": after, "state": _state(observation, selection=True),
                        "time": utc_now(),
                    })
                self._change(epoch, pending_steps=[])
                unknown = result.before is None or result.after is None
                unknown = unknown or any(
                    item.get("snapshot", {}).get("active_sketch") and item.get("active_sketch_geometry_complete") is False
                    for item in (observation, after)
                )
                changed = not _equivalent(_state(observation), _state(after))
                self._feedback(epoch, "step_failed", result.message, result=result.to_dict(),
                               state_known=not unknown, state_uncertain=unknown)
                if not self._alive(epoch):
                    return
                category = self._task.get("last_failure", {}).get("failure_category")
                if category in {"safety", "unknown"}:
                    self._change(epoch, dirty_failure={"before_observation": observation, "result": result.to_dict()})
                    execution = result.data.get("execution")
                    if isinstance(execution, dict) and execution.get("started") is False:
                        self._review(epoch, "写入前检查未通过，本次命令尚未执行。请核对活动零件与配置；不要为本次命令执行撤销。",
                                     failure_category=category)
                    return  # Do not auto-activate even an owned trial when execution state is unknown.
                if (unknown or changed or call.tool == "sketch.set_circle_radius"
                        or result.data.get("circle_edit", {}).get("requires_review")
                        or result.data.get("mutation", {}).get("requires_review")
                        or result.error_code in {"PostconditionFailed", "StateChanged", "DocumentChanged", "CandidateStateChanged"}):
                    self._change(epoch, dirty_failure={"before_observation": observation,
                                                      "result": result.to_dict()})
                    if (self._task.get("active_trial") and result.error_code not in {
                            "StateChanged", "DocumentChanged", "CandidateStateChanged"}):
                        self._event("trial_step_failed", epoch, message="候选步骤未通过，正在验证是否能安全返回保留的原模型。")
                    else:
                        if result.data.get("execution", {}).get("started") is False:
                            self._review(epoch, "写入前检查未通过，本次命令尚未执行。请核对活动零件与配置；不要为本次命令执行撤销。")
                        else:
                            self._review(epoch, "失败步骤的现场状态需要确认；请检查，必要时撤销到失败前状态。",
                                         failure_category="adapter_contract" if category == "adapter_contract" else "unknown")
                    return
                if self._task.get("status") == "review":
                    return
                self._change(epoch, expected_observation=after)
                self._diagnose(epoch, "selection" if call.tool.startswith(("selection.", "sketch.begin")) else "candidate")
                return
            with self._lock:
                if epoch != self._epoch:
                    return
                self._task["completed_steps"].append(call.to_dict())
                self._task["evidence"].append(result.to_dict())
                applied = result.data.get("applied", {})
                if (call.tool == "sketch.circle" and applied.get("creation_verified") is True
                        and applied.get("entity_ref") and applied.get("sketch_ref")):
                    self._task.setdefault("created_circles", []).append({
                        "entity_ref": applied["entity_ref"], "sketch_ref": applied["sketch_ref"],
                        "document_identity": after.get("document_identity"), "document": list(_identity(after)),
                        "sketch_name": after["snapshot"]["active_sketch"], "step_id": call.id,
                        "radius_mm": applied.get("radius_mm"), "center_sketch_m": applied.get("center_sketch_m"),
                    })
                if call.tool == "sketch.set_circle_radius":
                    for circle in self._task.get("created_circles", []):
                        if circle.get("entity_ref") == call.arguments["entity_ref"]:
                            circle["radius_mm"] = applied.get("radius_mm")
                            circle["last_edit_step_id"] = call.id
                if call.tool in {"feature.extrude", "feature.revolve"}:
                    previous = {(item.get("name"), item.get("type")) for item in observation.get("features", [])}
                    for feature in after.get("features", []):
                        if (feature.get("name"), feature.get("type")) not in previous:
                            self._task.setdefault("created_features", []).append({
                                "name": feature.get("name"), "type": feature.get("type"), "step_id": call.id,
                            })
            updates: dict[str, Any] = {"expected_observation": after,
                                       "pending_steps": [item.to_dict() for item in calls[index + 1:]]}
            if call.tool == "document.new_part":
                updates["bound_document"] = list(_identity(after))
            if call.tool in {"feature.extrude", "feature.revolve"}:
                updates["gateway_context_reset"] = False
            self._change(epoch, **updates)
            if not _equivalent(_state(observation, selection=True), _state(after, selection=True)):
                geometry_changed = (call.tool in {"sketch.circle", "sketch.rectangle", "sketch.polygon", "sketch.line",
                                                 "sketch.centerline", "sketch.set_circle_radius", "feature.extrude", "feature.revolve"}
                                    and not _equivalent(_state(observation), _state(after)))
                self._progress(epoch, "verified_geometry" if geometry_changed else "verified_model", _state(after))
            self._event("step_succeeded", epoch, call=call.to_dict(), result=result.to_dict(),
                        **({"message": f"试验步骤已验证（尚未采用整个候选）：{call.description or call.tool}"}
                           if self._task.get("active_trial") else {}))
        if self._alive(epoch) and not self._task.get("active_trial"):
            if calls and len(self._task.get("progress_fingerprints", [])) == progress_before:
                self._feedback(epoch, "strategy_no_progress", "本阶段未形成新的已验证状态，请调整恢复策略，避免重复无效果操作。")
            self._feedback(epoch, "stage_succeeded", "本阶段操作已通过本地后验检查，请判断下一阶段。")
            self._event("stage_completed", epoch)

    def _authorize_circle_edit(self, call: ToolCall, observation: dict[str, Any], epoch: int) -> bool:
        """No index/name-based ownership inference and no adoption of existing geometry."""
        owned = next((item for item in self._task.get("created_circles", [])
                      if item.get("entity_ref") == call.arguments.get("entity_ref")), None)
        if owned is None:
            self._review(epoch, "该圆缺少本任务已验证的创建引用，不能授权修改。旧版任务的圆不能仅按枚举顺序接管；请新建任务或人工处理。")
            return False
        if (not observation.get("document_identity") or owned.get("document") != list(_identity(observation))
                or owned.get("sketch_name") != observation["snapshot"].get("active_sketch")
                or observation.get("active_sketch_geometry_complete") is not True):
            self._review(epoch, "圆所属文档、活动草图或完整几何证据无法确认，暂停半径修改。")
            return False
        result = self._investigate("sketch.inspect", {}, automatic=True)
        if not self._alive(epoch) or self._task["status"] == "review":
            return False
        if result.get("error_code") == "RoundDiagnosticLimit":
            self._change(epoch, pending_steps=[])
            self._feedback(epoch, "preflight_recoverable", "本轮调查额度已用完；下一轮需重新读取圆实体证据后才能修改半径。")
            return False
        sketch = result.get("data", {}).get("sketch") or {}
        if (not result.get("ok") or result.get("data", {}).get("complete") is not True
                or sketch.get("geometry_complete") is not True or sketch.get("sketch_ref") != owned.get("sketch_ref")
                or sketch.get("document_identity") != observation.get("document_identity")):
            self._review(epoch, "无法取得同一文档、同一草图的完整实体调查证据，暂停半径修改。")
            return False
        matches = [item for item in sketch.get("segments", []) if item.get("entity_ref") == owned["entity_ref"]]
        if len(matches) != 1 or matches[0].get("is_circle") is not True or matches[0].get("editable") is not True:
            self._review(epoch, "目标圆的唯一引用或本会话可编辑权限已失效，不能自动接管。")
            return False
        circle = matches[0]
        if (circle.get("sketch_ref") != owned["sketch_ref"]
                or not _equivalent(circle.get("center_sketch_m"), owned.get("center_sketch_m"), "center_sketch_m")):
            self._review(epoch, "目标圆草图引用或圆心已改变，暂停修改并检查现场。")
            return False
        if not self._number(circle.get("radius_mm"), call.arguments["expected_radius_mm"], "eq", 1e-6):
            self._change(epoch, pending_steps=[])
            self._feedback(epoch, "policy_rejected", "expected_radius_mm 与最新实测半径不符；请根据 sketch.inspect 修正候选参数。",
                           actual_radius_mm=circle.get("radius_mm"), entity_ref=owned["entity_ref"])
            return False
        fresh = self._observe(epoch)
        return bool(fresh and self._check_expected(fresh, epoch))

    @classmethod
    def _valid_extent_measurement(cls, data: Any, observation: dict[str, Any]) -> bool:
        if not isinstance(data, dict) or observation.get("complete") is not True:
            return False
        snapshot = observation.get("snapshot", {})
        identity = observation.get("document_identity")
        if (not isinstance(identity, str) or not identity or data.get("document_identity") != identity
                or data.get("complete") is not True or data.get("issues") != []
                or data.get("source") != "IBody2.GetExtremePoint" or data.get("coordinate_system") != "model"
                or snapshot.get("connected") is not True or snapshot.get("document_type") != "part"
                or data.get("document_title") != snapshot.get("document_title")
                or data.get("document_path") != snapshot.get("document_path")):
            return False
        for key in ("configuration", "configuration_identity"):
            if not isinstance(data.get(key), str) or not data[key]:
                return False
            if data[key] != observation.get(key):
                return False
        count = data.get("bodies_count")
        if (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 64
                or count != snapshot.get("body_count")):
            return False
        extents = data.get("extents_mm")
        return isinstance(extents, dict) and all(
            isinstance(extents.get(axis), (int, float)) and not isinstance(extents[axis], bool)
            and math.isfinite(extents[axis]) and extents[axis] > 0 for axis in ("x", "y", "z")
        )

    def _measure_extents_for_verification(self, observation: dict[str, Any], epoch: int) -> dict[str, Any] | None:
        """Fresh host verification is bounded independently from Agent investigation quota."""
        if not self._alive(epoch):
            return None
        checks = self._task.get("extent_checks", 0)
        if checks >= self.max_rounds:
            self._review(epoch, "外廓验收次数已达到本任务轮次上限，请根据已有证据检查模型。", "capability")
            return None
        call = ToolCall("model.measure_extents", {})
        try:
            call = self.registry.validate(call)
            if self.registry.write_scope(call.tool) != "none":
                raise PermissionError("外廓验收只能使用已注册的只读测量工具。")
            allowed, reason = self.preflight(call)
            if not self._alive(epoch):
                return None
            if not allowed:
                raise PermissionError(reason or "当前不允许读取模型外廓。")
            self._change(epoch, extent_checks=checks + 1)
            self._event("investigation_started", epoch, tool=call.tool, arguments={}, origin="verification")
            if not self._alive(epoch):
                return None
            result = self.worker.call(call, 60)
            if not self._alive(epoch):
                return None
            data = copy.deepcopy(result.data)
            try:
                _signature(data)
            except (ValueError, TypeError):
                data = {"complete": False, "issues": ["外廓测量包含非有限值或无效结构，证据不可使用。"]}
            if not result.ok:
                data["complete"] = False
            output = {"ok": result.ok, "tool": call.tool, "arguments": {}, "message": result.message,
                      "error_code": result.error_code, "data": data, "duration_ms": result.duration_ms}
        except PermissionError as exc:
            self._review(epoch, str(exc), "safety")
            return None
        except Exception as exc:
            if not self._alive(epoch):
                return None
            data = {"complete": False, "issues": [str(exc)[:1000]]}
            output = {"ok": False, "tool": call.tool, "arguments": {}, "message": str(exc)[:1000],
                      "error_code": type(exc).__name__, "data": data, "duration_ms": 0}
        with self._lock:
            if not self._alive(epoch):
                return None
            self._task.setdefault("diagnostic_history", []).append({
                **copy.deepcopy(output), "time": utc_now(), "origin": "verification",
                "fingerprint": _signature(_evidence_value(output)),
            })
            self._change(epoch)
        self._event("investigation_completed", epoch, result=output, origin="verification")
        fresh = self._observe(epoch, recoverable=True, include_configuration=True)
        if fresh is None or fresh.get("complete") is not True or not self._check_expected(fresh, epoch):
            return None
        if not _equivalent(_state(observation), _state(fresh)):
            self._review(epoch, "外廓测量前后文档或几何状态发生变化，不能使用混合现场证据。", "safety")
            return None
        if any(not isinstance(observation.get(key), str) or not observation[key]
               or observation[key] != fresh.get(key) for key in ("configuration", "configuration_identity")):
            self._review(epoch, "外廓验收前后配置身份未知或发生变化，不能使用混合配置的证据。", "safety")
            return None
        if data.get("document_identity") is not None and data.get("document_identity") != observation.get("document_identity"):
            self._review(epoch, "外廓测量返回另一文档的身份，不能继续验收。", "safety")
            return None
        fresh["extent_measurement"] = data
        self._change(epoch, latest_observation=fresh)
        return fresh

    def _complete(self, epoch: int) -> None:
        if self._consume_steering(epoch):
            return
        revision = self._task.get("steering_revision", 0)
        issues = contract_issues(self._task.get("requirements", []), self._task["goals"])
        if issues:
            self._feedback(epoch, "verification_failed", "原始需求的验收合同尚未完整：" + "；".join(issues)[:1000], contract_issues=issues)
            return
        has_extents = any(goal["metric"] == "extent_mm" for goal in self._task["goals"])
        observation = self._observe(epoch, recoverable=True, include_configuration=has_extents)
        if observation is None or observation.get("complete") is not True or not self._check_expected(observation, epoch):
            return
        if has_extents:
            observation = self._measure_extents_for_verification(observation, epoch)
            if observation is None:
                return
        if self._consume_steering(epoch):
            return
        snapshot = observation["snapshot"]
        results = [self._check_goal(GoalCheck.model_validate(goal), observation, self._task.get("created_features", []))
                   for goal in self._task["goals"]]
        geometry_issues = contract_issues(
            self._task.get("requirements", []), self._task["goals"],
            verified_geometry_ids={item["id"] for item in results if item["passed"] and item["geometry_evidence"]},
        )
        errors = self._feature_errors(observation)
        part_requested = any(goal["metric"] == "document_type" and goal["expected"] == "part"
                             for goal in self._task["goals"]) or any(
                                 step["tool"] == "document.new_part" for step in self._task["completed_steps"])
        implicit = [
            {"id": "requirements_have_geometry_evidence", "passed": not geometry_issues,
             "observable": True, "issues": geometry_issues},
            {"id": "task_created_document", "passed": any(step["tool"] == "document.new_part" for step in self._task["completed_steps"]), "observable": True},
            {"id": "no_active_sketch", "passed": not snapshot.get("active_sketch"), "observable": True},
            {"id": "no_feature_errors", "passed": errors == 0, "observable": errors is not None},
            {"id": "part_has_body", "passed": not part_requested or self._number(snapshot.get("body_count"), 1, "ge", 0),
             "observable": not part_requested or snapshot.get("body_count") is not None},
            {"id": "connected_document", "passed": snapshot.get("connected") is True and snapshot.get("document_type") in {"part", "assembly", "drawing"}, "observable": True},
        ]
        verification = {"checked_at": utc_now(), "goals": results, "implicit": implicit,
                        "requirements": copy.deepcopy(self._task.get("requirements", [])),
                        "passed": bool(results) and all(item["passed"] for item in results + implicit)}
        self._change(epoch, verification=verification)
        if not all(item["observable"] for item in results + implicit):
            attempts = self._task.setdefault("autonomy_attempts", {})
            attempts["verification"] = attempts.get("verification", 0) + 1
            if attempts["verification"] > 2:
                pending = [item for item in results if item["metric"] == "dimension_mm" and not item["observable"]]
                detail = (" 未读取到尺寸：" + "、".join(f"{item['id']}（{item['description']}）" for item in pending)[:600]
                          + "。草图创建不自动产生命名尺寸，不能用拉伸深度替代草图边长或删除、改换原验收目标。"
                          if pending else "")
                self._review(epoch, "部分验收目标经调查仍无法从模型中可靠读取，需要补充读取能力或人工决定。" + detail, "capability")
            else:
                self._feedback(epoch, "verification_unobservable", "部分目标尚未取得可验证证据，请定点调查、绑定真实尺寸或细化验收指标。",
                               verification=verification)
                self._diagnose(epoch, "verification")
        elif verification["passed"]:
            with self._lock:
                if revision != self._task.get("steering_revision", 0):
                    self._consume_steering(epoch)
                    return
                self._change(epoch, status="completed", pending_steps=[], message="最终模型已通过锁定目标和模型健康检查。")
            self._event("task_completed", epoch, verification=verification)
        else:
            self._feedback(epoch, "verification_failed", "模型尚未满足最终验收目标，请根据验证结果继续修正。",
                           verification=verification)

    @staticmethod
    def _feature_errors(observation: dict[str, Any]) -> int | None:
        features = observation.get("features")
        if not isinstance(features, list) or any(not isinstance(item, dict) or item.get("error_code") is None for item in features):
            return None
        return sum(item["error_code"] != 0 for item in features)

    @staticmethod
    def _number(actual: Any, expected: float, operator: str, tolerance: float) -> bool:
        if isinstance(actual, (bool, str)) or not isinstance(actual, (float, int)) or not math.isfinite(actual):
            return False
        if operator == "ge":
            return actual >= expected - tolerance
        if operator == "le":
            return actual <= expected + tolerance
        return abs(actual - expected) <= tolerance

    @classmethod
    def _check_goal(cls, goal: GoalCheck, observation: dict[str, Any],
                    created_features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        snapshot = observation["snapshot"]
        actual: Any = None
        feature_type: str | None = None
        geometry_evidence = goal.metric in {"volume_mm3", "dimension_mm", "extent_mm"}
        if goal.metric in {"body_count", "feature_count", "document_type"}:
            actual = snapshot.get(goal.metric)
        elif goal.metric == "volume_mm3":
            volume = snapshot.get("volume_m3")
            actual = volume * 1_000_000_000 if isinstance(volume, (int, float)) else None
        elif goal.metric == "dimension_mm":
            matches = [item.get("value_mm") for item in observation.get("dimensions", [])
                       if item.get("name") == goal.target]
            actual = matches[0] if len(matches) == 1 else None
        elif goal.metric == "extent_mm":
            measurement = observation.get("extent_measurement", {})
            if cls._valid_extent_measurement(measurement, observation):
                actual = measurement["extents_mm"][goal.target]
        elif goal.metric == "feature_present":
            features = observation.get("features")
            if isinstance(features, list):
                matches = [item for item in features if item.get("name") == goal.target]
                actual = bool(matches)
                if len(matches) == 1:
                    feature_type = matches[0].get("type")
                    geometry_evidence = bool(
                        isinstance(feature_type, str) and feature_type.casefold() in _SOLID_FEATURE_TYPES
                        and any(item.get("name") == goal.target and item.get("type") == feature_type
                                for item in created_features or [])
                    )
        elif goal.metric == "feature_errors":
            actual = cls._feature_errors(observation)
        observable = actual is not None and goal.binding == "fixed"
        if goal.metric == "feature_present" and actual is True and not feature_type:
            observable = False
        if isinstance(actual, float) and not math.isfinite(actual):
            observable = False
        if goal.metric in {"document_type", "feature_present"}:
            passed = observable and actual == goal.expected
        else:
            passed = observable and cls._number(actual, float(goal.expected), goal.operator, goal.tolerance)
        return {"id": goal.id, "description": goal.description, "metric": goal.metric,
                "target": goal.target, "expected": goal.expected, "actual": actual,
                "operator": goal.operator, "tolerance": goal.tolerance,
                "observable": observable, "passed": passed, "geometry_evidence": geometry_evidence,
                "feature_type": feature_type}
