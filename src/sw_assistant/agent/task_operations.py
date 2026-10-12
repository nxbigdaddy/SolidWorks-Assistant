"""Native agent stages enter the same serial policy and verification boundary."""
from __future__ import annotations

import copy
import json
import threading
from typing import Any

from ..domain import utc_now
from .security import scrub_agent_value
from .failure_evidence import failure_geometry_summary
from .task_models import TaskDecision


class NativeStageRunnerMixin:
    def _init_native_stages(self: Any) -> None:
        self._native_run_scope = False
        self._native_single_stage = False
        self._native_decide_active = False
        self._native_decision_epoch = -1
        self._native_decision_revision = -1
        self._native_stage_attempts = 0
        self._native_stage_executions = 0
        self._native_stage_inflight = None

    def _native_stage_capabilities(self: Any) -> dict[str, Any]:
        allowed = (self._native_run_scope and self._native_decide_active
                   and self._busy.locked() and self._busy_owner == threading.get_ident()
                   and self._native_decision_epoch == self._epoch and self._alive(self._epoch)
                   and self._task.get("status") == "running" and not self._native_stage_inflight
                   and not self._task.get("pending_messages")
                   and self._task.get("steering_revision", 0) == self._native_decision_revision)
        limit = 1 if self._native_single_stage else 2
        return {"allowed": bool(allowed and self._native_stage_attempts < limit),
                "max_stages": limit, "remaining_stages": max(0, limit - self._native_stage_attempts),
                "stage_timeout_seconds": 600, "same_thread_required": True,
                "scope": "initialization_or_isolated_trial", "completion_requires_host_verification": True}

    def execute_agent_stage(self: Any, envelope: dict[str, Any]) -> dict[str, Any]:
        """Execute once, or report a proven cached result; never execute in a helper thread."""
        from .task_runner import _signature

        epoch = self._epoch
        request_id = envelope.get("request_id") if isinstance(envelope, dict) else None
        rejected = {"ok": False, "status": "denied", "request_id": request_id,
                    "mutation_attempted": False, "requires_review": False}
        # Scope checking precedes *all* reads, persistence, diagnostics and callbacks.
        if (not self._native_run_scope or not self._native_decide_active or not self._busy.locked()
                or self._busy_owner != threading.get_ident() or self._native_decision_epoch != epoch
                or not self._alive(epoch) or self._task.get("status") != "running"
                or self._native_stage_inflight):
            return {**rejected, "error_code": "StageScopeDenied", "message": "当前不是允许执行阶段的任务线程安全边界。"}
        if (self._task.get("pending_messages")
                or self._task.get("steering_revision", 0) != self._native_decision_revision):
            return {**rejected, "error_code": "StageContextStale", "message": "用户补充了要求，须先重新决策，未执行旧候选。"}
        counted = False
        try:
            if (not isinstance(envelope, dict) or set(envelope) != {"request_id", "decision"}
                    or not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
                raise ValueError("阶段关联标识或请求结构不合法")
            encoded = json.dumps(envelope["decision"], ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 32_768:
                raise ValueError("阶段决策超过 32 KB")
            digest = _signature(envelope["decision"])
            ledger = self._task.setdefault("agent_stage_requests", {})
            previous = ledger.get(request_id)
            if previous is not None:
                if (previous.get("task_id") != self._task["id"] or previous.get("epoch") != epoch
                        or previous.get("round") != self._task["rounds"] or previous.get("digest") != digest):
                    self._native_stage_attempts += 1
                    return {**rejected, "error_code": "StageReplayRejected", "message": "该请求已使用，拒绝变更内容或跨检查点重放。"}
                cached = previous.get("result")
                if isinstance(cached, dict):
                    return copy.deepcopy(cached)
                return {**rejected, "status": "unknown", "mutation_attempted": True, "requires_review": True,
                        "error_code": "StageOutcomeUnknown", "message": "该请求已提交但结果未知，禁止重放。"}
            self._native_stage_attempts += 1
            counted = True
            limit = 1 if self._native_single_stage else 2
            if self._native_stage_attempts > limit:
                return {**rejected, "error_code": "StageBudgetExceeded", "message": "本轮阶段尝试额度已用完。"}
            if len(ledger) >= 128:
                return {**rejected, "error_code": "StageLedgerFull", "message": "请求去重记录达到上限，请在检查点建立新任务。"}
            decision = TaskDecision.model_validate(envelope["decision"])
            if decision.action != "act":
                raise ValueError("阶段工具仅接受 act；完成必须由宿主独立验收")
            initialization = {"session.ensure", "document.new_part"}
            if decision.trial is None and any(step.tool not in initialization for step in decision.steps):
                raise ValueError("原生建模工具只接受独立初始化或带 trial 的完整副本候选阶段")
            if decision.trial is None and any(step["tool"] == "document.new_part" for step in self._task["completed_steps"]):
                raise ValueError("本任务已经初始化，禁止重建文档覆盖成功前缀")
            if self._task["actions"] + len(decision.steps) > self.max_rounds * 12:
                return {**rejected, "error_code": "ActionBudgetExceeded", "message": "剩余任务操作预算不足以执行完整阶段。"}
        except (TypeError, ValueError) as exc:
            # Invalid input still consumes a stage attempt, even when parsing fails.
            if not counted:
                self._native_stage_attempts += 1
            return {**rejected, "error_code": "InvalidStageDecision", "message": str(exc)[:1500]}

        record: dict[str, Any] = {
            "task_id": self._task["id"], "epoch": epoch, "round": self._task["rounds"], "digest": digest,
            "status": "submitted", "created_at": utc_now(), "request_id": request_id,
        }
        ledger[request_id] = record
        self._change(epoch)
        before_actions = self._task["actions"]
        before_completed = len(self._task["completed_steps"])
        before_trials = len(self._task.get("trial_history", []))
        before_feedback = len(self._task["feedback"])
        before_failures = len(self._task.get("failure_attempts", []))
        self._native_stage_inflight = request_id

        def expire() -> None:
            # Timer never performs CAD. stop() invalidates the epoch and interrupts
            # the worker; the original callback remains on its owning task thread.
            with self._lock:
                if self._native_stage_inflight != request_id or not self._alive(epoch):
                    return
                self._task["native_stage_timeout"] = {"request_id": request_id, "seconds": 600}
            self.stop()

        watchdog = threading.Timer(600, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            accepted = self._accept(decision, epoch)
            if accepted and self._alive(epoch) and self._task.get("status") == "running":
                self._native_stage_executions += 1
                record["status"] = "executing"
                self._change(epoch)
                if decision.trial is not None:
                    self._execute_trial(decision, epoch)
                else:
                    self._execute_stage(epoch)
            result = self._native_stage_result(request_id, epoch, before_actions, before_completed,
                                               before_trials, before_feedback, accepted, len(decision.steps), before_failures)
        except Exception as exc:
            if self._alive(epoch):
                self._review(epoch, f"原生阶段执行状态无法确认，禁止重复执行：{exc}")
            result = {"ok": False, "status": "unknown", "request_id": request_id,
                      "mutation_attempted": True, "requires_review": True,
                      "error_code": "StageOutcomeUnknown", "message": str(exc)[:1500]}
        finally:
            watchdog.cancel()
            self._native_stage_inflight = None
        with self._lock:
            # stop() changed the epoch: preserve the unknown outcome in the stopped
            # task record, but never reactivate CAD, the task or pending steps.
            record.update(status=result["status"], result=copy.deepcopy(result), finished_at=utc_now())
            self._change(self._epoch)
        if self._native_single_stage and self._native_stage_executions and self._alive(epoch) and self._task.get("status") == "running":
            self._change(epoch, status="ready", pending_decision=None, pending_steps=[],
                         message="本阶段已结束；继续时将基于实际结果重新决策。")
        self._event("native_stage_finished", self._epoch, request_id=request_id, status=result["status"], ok=result["ok"])
        return result

    def _native_stage_result(self: Any, request_id: str, epoch: int, before_actions: int,
                             before_completed: int, before_trials: int, before_feedback: int,
                             accepted: bool, expected_steps: int, before_failures: int) -> dict[str, Any]:
        task = self.snapshot()
        attempted = (task["actions"] > before_actions or bool(task.get("active_trial"))
                     or len(task.get("trial_history", [])) > before_trials)
        outcome, passed = "denied", False
        if not self._alive(epoch):
            outcome = "stopped"
        elif task.get("status") == "review" or task.get("dirty_failure") or task.get("active_trial"):
            outcome = "unknown"
        elif len(task.get("trial_history", [])) > before_trials:
            outcome = "trial_" + task["trial_history"][-1]["status"]
            passed = outcome == "trial_committed"
        elif (accepted and not task.get("pending_steps")
              and len(task["completed_steps"]) - before_completed == expected_steps):
            outcome, passed = "passed", True
        elif accepted:
            outcome = "not_committed"
        observation = task.get("latest_observation", {})
        trial = task.get("trial_history", [])[-1] if len(task.get("trial_history", [])) > before_trials else {}
        result = scrub_agent_value({
            "ok": passed, "status": outcome, "request_id": request_id, "task_status": task.get("status"),
            "mutation_attempted": attempted, "requires_review": outcome in {"unknown", "stopped"},
            "message": task.get("message", ""), "actions_used": task["actions"] - before_actions,
            "completed_step_count": len(task["completed_steps"]),
            "completed_steps": task["completed_steps"][before_completed:],
            "observation": {key: observation.get(key) for key in ("snapshot", "document_identity", "complete", "issues")},
            "observation_details_omitted": True,
            "feedback": [{key: item.get(key) for key in ("kind", "message", "round")}
                         for item in task["feedback"][before_feedback:][-5:]],
            "trial": {key: trial.get(key) for key in ("status", "checks", "failure", "finish_receipt")} if trial else None,
            "failure_evidence": [failure_geometry_summary(item)
                                 for item in task.get("failure_attempts", [])[before_failures:][-1:]],
            "next_action": ("停止写入并等待宿主核对现场，不得重放。" if outcome in {"unknown", "stopped"} else
                            "使用实测结果继续调查或提交不同候选；已成功步骤不得重放，最终完成仍需宿主独立验收。"),
        })
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 56_000:
            result["trial"] = {"status": trial.get("status"), "details_omitted": True,
                               "finish_receipt": result["trial"].get("finish_receipt") if result["trial"] else None}
            result["completed_steps"] = []
            result["completed_steps_omitted"] = True
        return dict(result)
