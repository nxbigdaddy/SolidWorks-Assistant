"""Host-owned speculative CAD programs; never undo an unknown user's operation."""
from __future__ import annotations

import copy
import math
from typing import Any

from ..domain import ToolCall, utc_now
from .security import scrub_agent_value
from .task_models import TaskDecision
from .task_checkpoints import CheckpointRunnerMixin


_TENTATIVE_FIELDS = (
    "completed_steps", "evidence", "created_features", "created_circles", "goals",
    "goal_bindings", "contract_audit", "progress_fingerprints", "gateway_context_reset",
)


def _finish_receipt(result: Any) -> dict[str, Any]:
    """Keep bounded scalar RPC metadata, never duplicate CAD observations or paths."""
    if not isinstance(result, dict):
        return {"ok": False, "status": "invalid_response", "reason": "finish 返回值不是对象"}
    receipt: dict[str, Any] = {}
    for field in ("ok", "status", "reason", "error_code", "checkpoint_id", "checkpoint_parent_id",
                  "persisted_current_state", "checkpoint_preserved", "native_context_restored",
                  "baseline_unchanged", "recovery_mode"):
        if field not in result:
            continue
        value = result[field]
        if value is None or isinstance(value, bool):
            receipt[field] = value
        elif isinstance(value, str):
            receipt[field] = scrub_agent_value(value)[:2000 if field == "reason" else 300]
        elif isinstance(value, int) and -(2 ** 63) <= value < 2 ** 63:
            receipt[field] = value
        elif isinstance(value, float) and math.isfinite(value):
            receipt[field] = value
        else:
            receipt[field] = "<invalid scalar>"
    return receipt


def _finish_issues(result: Any, wanted: dict[str, Any], accepted: bool) -> list[str]:
    """Explain the existing strict finish checks without changing acceptance criteria."""
    from .task_runner import _equivalent, _state

    if not isinstance(result, dict):
        return ["finish 返回值不是对象"]
    issues: list[str] = []
    expected_status = "committed" if accepted else "restored"
    if result.get("ok") is not True:
        issues.append("finish.ok 不是 true")
    if result.get("status") != expected_status:
        status = result.get("status")
        shown = status[:100] if isinstance(status, str) else "<missing or invalid>"
        issues.append(f"finish.status={shown}，应为 {expected_status}")
    observation = result.get("observation")
    if not isinstance(observation, dict):
        return [*issues, "observation 缺失或不是对象"]
    if observation.get("complete") is not True:
        issues.append("observation.complete 不是 true")
    try:
        expected_state, observed_state = _state(wanted), _state(observation)
        if not _equivalent(expected_state, observed_state):
            changed = [key for key in sorted(set(expected_state) | set(observed_state))
                       if key not in expected_state or key not in observed_state
                       or not _equivalent(expected_state[key], observed_state[key], key)]
            issues.append("模型状态字段不匹配：" + ", ".join(changed[:20]))
    except (AttributeError, TypeError, ValueError) as exc:
        issues.append("observation 状态无法读取：" + str(exc)[:300])
    if "model_update_stamp" in wanted and wanted["model_update_stamp"] != observation.get("model_update_stamp"):
        issues.append("model_update_stamp 不匹配")
    for field in ("configuration", "configuration_identity"):
        if not wanted.get(field) or wanted.get(field) != observation.get(field):
            issues.append(field + " 缺失或不匹配")
    return issues


class TrialRunnerMixin(CheckpointRunnerMixin):
    def _trial_capabilities(self: Any) -> dict[str, Any]:
        enabled = (getattr(self.worker, "candidate_workspace_enabled", False) is True
                   and callable(getattr(self.worker, "candidate_request", None)))
        return {
            "enabled": enabled, "mode": "isolated_part_copy", "max_steps": 12,
            "requires": ["task_owned_part", "complete_feature_boundary", "single_configuration",
                         "no_external_references", "measurable_trial_checks"],
            "recovery": "Switch to unchanged baseline and independently verify; never Undo or close documents.",
            "limits": ["existing tool vocabulary only; no arbitrary code or COM",
                       "no assembly / drawing / active-sketch branching",
                       "no inherited terminal-face or editable-entity references",
                       "candidate files and open documents retained locally for inspection"],
        }

    def _execute_trial(self: Any, decision: TaskDecision, epoch: int) -> None:
        from .task_runner import _equivalent, _identity, _signature, _state

        if decision.trial is None:
            raise ValueError("trial specification is required")
        if not self._alive(epoch):
            return
        if not self._trial_capabilities()["enabled"]:
            self._change(epoch, pending_steps=[])
            self._feedback(epoch, "policy_rejected", "当前工作进程不支持独立副本试验；没有执行候选步骤。")
            return
        if self._task.get("active_trial"):
            self._review(epoch, "存在未确认的试验副本；请保留现场并新建任务，不能自动接管旧试验。")
            return
        baseline = self._observe(epoch, include_configuration=True)
        if not baseline or not self._check_expected(baseline, epoch):
            return
        calls = copy.deepcopy(self._task["pending_steps"])
        program = [{"tool": item["tool"], "arguments": item["arguments"]} for item in calls]
        key = _signature({"baseline": _state(baseline), "program": program})
        if any(item.get("fingerprint") == key and item.get("status") == "restored"
               for item in self._task.get("trial_history", [])):
            self._change(epoch, pending_steps=[])
            self._feedback(epoch, "repeat_blocked", "该候选程序已在同一基线上失败；请修改步骤或参数，改理由/检查条件不算新方案。")
            return
        if not any(step.get("tool") == "document.new_part" for step in self._task["completed_steps"]):
            self._feedback(epoch, "policy_rejected", "试验必须先单独初始化本任务新零件，不能操作既有用户文档。")
            return
        snapshot = baseline["snapshot"]
        if snapshot.get("active_sketch") or snapshot.get("selection_count"):
            self._change(epoch, pending_steps=[])
            self._feedback(epoch, "policy_rejected", "试验须从完整特征边界开始，不能在活动草图或有选中对象时分支。")
            return
        # Even the private file-copy boundary must obey the local permission / stop gate.
        try:
            allowed, reason = self.preflight(ToolCall("document.new_part", {}))
        except Exception as exc:
            allowed, reason = False, str(exc)
        if not self._alive(epoch):
            return
        if not allowed:
            self._review(epoch, reason or "当前不允许开始独立副本试验。")
            return
        backup = {name: copy.deepcopy(self._task.get(name)) for name in _TENTATIVE_FIELDS}
        failures_before = len(self._task.get("failure_attempts", []))
        record = {"fingerprint": key, "program": program, "trial": decision.trial.model_dump(mode="json"),
                  "status": "starting", "started_at": utc_now(), "baseline": copy.deepcopy(baseline)}
        self._change(epoch, active_trial=record)
        self._event("trial_started", epoch, message="保留当前模型，正在创建独立试验副本。")
        if not self._alive(epoch):
            return
        try:
            opened = self.worker.candidate_request("begin", expected=baseline, task_id=self._task["id"])
        except Exception as exc:
            if self._alive(epoch):
                self._review(epoch, f"无法确认副本试验的启动状态；已保留现场：{exc}")
            return
        if not self._alive(epoch):
            return
        candidate = opened.get("candidate")
        if opened.get("ok") is not True or not opened.get("token") or not isinstance(candidate, dict):
            record.update(opening_error=str(opened.get("reason") or "invalid candidate response"),
                          opening_recovery=opened.get("recovery"))
            self._change(epoch, active_trial=record)
            if (opened.get("baseline_unchanged") is True and isinstance(opened.get("observation"), dict)
                    and opened["observation"].get("complete") is True
                    and _equivalent(_state(baseline), _state(opened["observation"]))):
                self._change(epoch, active_trial=None, pending_steps=[])
                self._feedback(epoch, "preflight_recoverable", "试验未开始，原模型未改变：" + str(opened.get("reason", "能力不可用")))
            else:
                self._review(epoch, "试验副本无法可靠建立，必须检查原件与候选文档：" + record["opening_error"][:1200])
            return
        if candidate.get("complete") is not True or not candidate.get("document_identity"):
            self._review(epoch, "试验副本身份或完整观察缺失，未执行建模步骤。")
            return
        record.update(token=opened["token"], status="running", candidate_identity=candidate["document_identity"])
        self._change(epoch, active_trial=record, expected_observation=candidate, latest_observation=candidate,
                     bound_document=list(_identity(candidate)), created_circles=[], gateway_context_reset=True)
        self._event("trial_ready", epoch, message="候选步骤仅在副本中执行；原模型保持不变。")
        self._execute_stage(epoch)
        if not self._alive(epoch):
            return  # Stop must not trigger hidden activation, copy, save or cleanup.
        # Unknown state, permissions and external changes are not recoverable by guessing a document.
        failure = self._task.get("dirty_failure") or {}
        code = failure.get("result", {}).get("error_code")
        if code in {"StateChanged", "DocumentChanged", "CandidateStateChanged"}:
            return
        if (self._task.get("status") == "review"
                and self._task.get("last_failure", {}).get("failure_category") in {"safety", "unknown"}):
            return
        failed_steps = self._task.get("failure_attempts", [])
        failure_result = (copy.deepcopy(failed_steps[-1]["result"])
                          if len(failed_steps) > failures_before else None)
        if self._task.get("status") == "review" and not failure_result:
            return
        current = self._task.get("latest_observation", {})
        if current.get("complete") is not True or _identity(current) != _identity(candidate):
            self._review(epoch, "候选状态未知或文档发生切换，未自动切回基线。")
            return
        result_checks: list[dict[str, Any]] = []
        accepted = (not failure_result and not self._task.get("pending_steps")
                    and self._task.get("status") == "running" and not self._task.get("pending_messages")
                    and len(self._task["completed_steps"]) == len(backup["completed_steps"]) + len(calls))
        if accepted and any(check.metric == "extent_mm" for check in decision.trial.checks):
            current = self._measure_extents_for_verification(current, epoch)
            if current is None or not self._alive(epoch) or self._task.get("status") == "review":
                return
        if accepted:
            result_checks = [self._check_goal(check, current, self._task.get("created_features", []))
                             for check in decision.trial.checks]
            has_geometry = any(check["passed"] and check.get("geometry_evidence") for check in result_checks)
            result_checks.extend([
                {"id": "actual_geometric_evidence", "passed": has_geometry},
                {"id": "closed_sketch", "passed": not current["snapshot"].get("active_sketch")},
                {"id": "healthy_features", "passed": self._feature_errors(current) == 0},
            ])
            accepted = all(check["passed"] for check in result_checks)
        record.update(checks=result_checks, failure=failure_result, status="checking")
        self._change(epoch, active_trial=record)
        self._event("trial_verifying", epoch, message=("候选检查通过，正在复核并采用副本。" if accepted
                                                       else "候选未通过，正在切回并独立复核原模型。"))
        if not self._alive(epoch):
            return
        try:
            result = self.worker.candidate_request("finish", token=opened["token"], accept=accepted,
                                                   expected=current)
        except Exception as exc:
            if self._alive(epoch):
                record["finish_receipt"] = _finish_receipt({
                    "ok": False, "status": "rpc_error", "reason": str(exc), "error_code": type(exc).__name__,
                })
                self._change(epoch, active_trial=record)
                self._review(epoch, "试验结束状态无法确认，已保留全部文档：" + record["finish_receipt"]["reason"])
            return
        if not self._alive(epoch):
            return
        wanted = current if accepted else baseline
        issues = _finish_issues(result, wanted, accepted)
        receipt = _finish_receipt(result)
        receipt["validation_issues"] = issues
        record["finish_receipt"] = receipt
        self._change(epoch, active_trial=record)
        if issues:
            reason = receipt.get("reason")
            detail = (f"结束回执原因：{reason}。" if isinstance(reason, str) and reason else "")
            detail += "；".join(issues)
            self._review(epoch, "试验恢复/采用结果没有通过独立状态比对，禁止继续写入。\n" + detail[:3500])
            return
        observation = result["observation"]
        record.update(status=result["status"], finished_at=utc_now())
        history = self._task.get("trial_history", []) + [record]
        paused = self._task.get("status") == "review"
        updates = {} if accepted else backup
        if not accepted and result.get("native_context_restored") is not True:
            updates.update(gateway_context_reset=True, created_circles=[])
        updates.update(active_trial=None, trial_history=history, expected_observation=observation,
                       latest_observation=observation, bound_document=list(_identity(observation)),
                       pending_steps=[], dirty_failure=None,
                       status="review" if paused else "running",
                       review_kind=self._task.get("review_kind", "") if paused else "")
        self._change(epoch, **updates)
        if accepted:
            if not self._record_checkpoint(result, observation, epoch):
                return
            self._progress(epoch, "verified_geometry", _state(observation))
            self._feedback(epoch, "trial_committed", "候选几何检查通过，已采用副本作为新基线；最终目标仍须独立验收。")
            self._event("trial_committed", epoch, message="试验通过：本阶段已采用，可继续下一阶段。")
        else:
            self._feedback(epoch, "trial_restored", "候选未通过，原模型已独立复核恢复。失败程序未进入成功前缀；请根据失败证据修改参数或更换构造路线。",
                           failure=failure_result, checks=result_checks, state_known=True)
            if not self._alive(epoch):
                return
            # Feedback may exhaust the no-progress budget and enter review.
            # Recovery is a fact; another autonomous attempt is not guaranteed.
            paused = self._task.get("status") != "running"
            message = ("已回到原模型并完成复核；自动推进已暂停，请先处理当前检查事项。" if paused
                       else "已回到原模型，Agent 将根据失败证据调整方案。")
            self._event("trial_restored", epoch, message=message, automatic_continuation=not paused)
