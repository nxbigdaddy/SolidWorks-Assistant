from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Callable

from ..domain import ExecutionState, Plan, StepResult, ToolCall, ToolError
from ..infrastructure import AuditLog
from ..solidworks.client import ToolWorker
from ..tools import ToolRegistry
from .recovery import assess_failure


EventCallback = Callable[[str, dict[str, Any]], None]
PreflightCallback = Callable[[ToolCall], tuple[bool, str]]


_LOCAL_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])[^\r\n;；，。\"'<>]*"
)


def _redact_recovery_value(value: Any, key: str = "") -> Any:
    """Remove local paths before a failure context is sent to a model service."""

    if "path" in key.casefold():
        return "<local-path>" if value else value
    if isinstance(value, dict):
        return {
            str(child_key): _redact_recovery_value(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_recovery_value(item) for item in value]
    if isinstance(value, str):
        return _LOCAL_PATH.sub("<local-path>", value)
    return value


class ExecutionEngine:
    def __init__(
        self,
        registry: ToolRegistry,
        worker: ToolWorker,
        audit: AuditLog,
        on_event: EventCallback | None = None,
        preflight: PreflightCallback | None = None,
    ) -> None:
        self.registry = registry
        self.worker = worker
        self.audit = audit
        self.on_event = on_event or (lambda _name, _payload: None)
        self.preflight = preflight or (lambda _call: (True, ""))
        self.plan: Plan | None = None
        self.cursor = 0
        self.state = ExecutionState.IDLE
        self.results: list[StepResult] = []
        self.skipped: set[int] = set()
        self.failure_history: list[dict[str, Any]] = []
        self._abort = False
        self._retry_fingerprints: Counter[str] = Counter()

    def load_plan(self, plan: Plan) -> None:
        if self.state == ExecutionState.RUNNING:
            raise ToolError("执行期间不能替换计划。")
        validated = self.registry.validate_plan(plan.steps)
        self.plan = Plan(
            plan.task,
            validated,
            plan.id,
            plan.created_at,
            list(plan.assumptions),
        )
        self.cursor = 0
        self.results.clear()
        self.skipped.clear()
        self.failure_history.clear()
        self._abort = False
        self._retry_fingerprints.clear()
        self.state = ExecutionState.READY
        self._emit("plan_loaded", plan=self.plan.to_dict())

    def execute_next(self, timeout_seconds: int = 120) -> StepResult | None:
        if self.plan is None or self.state not in {ExecutionState.READY, ExecutionState.PAUSED}:
            return None
        if self.state == ExecutionState.PAUSED:
            raise ToolError("当前步骤失败，请先选择恢复方式。")
        if self.cursor >= len(self.plan.steps):
            self._complete()
            return None
        if self._abort:
            self.state = ExecutionState.ABORTED
            self._emit("plan_aborted", cursor=self.cursor)
            return None
        call = self.plan.steps[self.cursor]
        try:
            allowed, reason = self.preflight(call)
        except Exception as exc:
            allowed, reason = False, f"执行前上下文检查失败：{exc}"
        if not allowed:
            snapshot = self.results[-1].after if self.results else None
            result = StepResult(
                False,
                call,
                reason or "当前 SolidWorks 上下文不允许执行该步骤。",
                error_code="ContextUnavailable",
                before=snapshot,
                after=snapshot,
            )
            self.results.append(result)
            self.state = ExecutionState.PAUSED
            self.audit.write(
                "tool_result",
                plan_id=self.plan.id,
                step_index=self.cursor,
                result=result.to_dict(),
            )
            self._emit("step_failed", index=self.cursor, result=result.to_dict())
            return result
        self.state = ExecutionState.RUNNING
        self._emit("step_started", index=self.cursor, call=call.to_dict())
        try:
            result = self.worker.call(call, timeout_seconds)
        except Exception as exc:
            result = StepResult(False, call, str(exc), error_code=type(exc).__name__)
        if result.ok:
            postcondition = self.registry.postcondition(call.tool)
            if postcondition is not None:
                ok, message = postcondition(call, result.data, result.before, result.after)
                if not ok:
                    result.ok = False
                    result.error_code = "PostconditionFailed"
                    result.message = message
        if not result.ok:
            recovery = assess_failure(result).to_dict()
            result.data = {**result.data, "recovery": recovery}
        self.results.append(result)
        self.audit.write(
            "tool_result",
            plan_id=self.plan.id,
            step_index=self.cursor,
            result=result.to_dict(),
        )
        if result.ok:
            index = self.cursor
            self.cursor += 1
            self.state = ExecutionState.ABORTED if self._abort else ExecutionState.READY
            self._emit("step_succeeded", index=index, result=result.to_dict())
            if self._abort:
                self._emit("plan_aborted", cursor=self.cursor)
            elif self.cursor >= len(self.plan.steps):
                self._complete()
        else:
            self.state = ExecutionState.ABORTED if self._abort else ExecutionState.PAUSED
            if self._abort:
                self._emit("plan_aborted", cursor=self.cursor)
            else:
                self._emit("step_failed", index=self.cursor, result=result.to_dict())
        return result

    def run_all(self, timeout_seconds: int = 120) -> None:
        while self.state == ExecutionState.READY and not self._abort:
            self.execute_next(timeout_seconds)

    def retry_failed(self, timeout_seconds: int = 120) -> StepResult | None:
        failure = self._failure()
        if self._model_changed_during_failure(failure):
            raise ToolError(
                "失败步骤已经改变了文档、草图或实体拓扑，不能原样重试；"
                "请先撤销/人工检查，或让模型根据当前状态重建后续步骤。"
            )
        fingerprint = self._fingerprint(failure)
        if self._retry_fingerprints[fingerprint] >= 1:
            raise ToolError("完全相同的失败已经重试过一次，请修改参数或环境。")
        self._retry_fingerprints[fingerprint] += 1
        self._archive_failure(failure, "retry")
        self.results.pop()
        self.state = ExecutionState.READY
        self._emit("retry_ready", index=self.cursor)
        return self.execute_next(timeout_seconds)

    def replace_failed(self, call: ToolCall) -> None:
        failure = self._failure()
        if self.plan is None:
            raise ToolError("没有可修改的计划。")
        raw_prefix = self.plan.steps[: self.cursor]
        effective_prefix = [
            step for index, step in enumerate(raw_prefix) if index not in self.skipped
        ]
        candidate_tail = [call, *self.plan.steps[self.cursor + 1 :]]
        validated = self.registry.validate_plan(effective_prefix + candidate_tail)
        validated_tail = validated[len(effective_prefix) :]
        replacement = validated_tail[0]
        self.plan.steps = raw_prefix + validated_tail
        self._archive_failure(failure, "replace_failed")
        self.results.pop()
        self.state = ExecutionState.READY
        self._emit("failed_step_replaced", index=self.cursor, call=replacement.to_dict())

    def replace_remaining(self, calls: list[ToolCall]) -> None:
        failure = self._failure()
        if self.plan is None or not calls:
            raise ToolError("替代计划不能为空。")
        raw_prefix = self.plan.steps[: self.cursor]
        effective_prefix = [
            step for index, step in enumerate(raw_prefix) if index not in self.skipped
        ]
        validated = self.registry.validate_plan(effective_prefix + calls)
        replacements = validated[len(effective_prefix) :]
        self.plan.steps = raw_prefix + replacements
        self._archive_failure(failure, "replace_remaining")
        self.results.pop()
        self.state = ExecutionState.READY
        self._emit(
            "remaining_replaced",
            index=self.cursor,
            count=len(replacements),
            calls=[call.to_dict() for call in replacements],
        )

    def recovery_context(self) -> dict[str, Any]:
        failure = self._failure()
        if self.plan is None:
            raise ToolError("没有可恢复的计划。")
        snapshot = failure.after.to_dict() if failure.after else {}
        snapshot.pop("document_path", None)
        before_snapshot = failure.before.to_dict() if failure.before else {}
        before_snapshot.pop("document_path", None)
        assessment = assess_failure(failure).to_dict()
        completed_steps = [
            call.to_dict()
            for index, call in enumerate(self.plan.steps[: self.cursor])
            if index not in self.skipped
        ]
        skipped_steps = [
            self.plan.steps[index].to_dict()
            for index in sorted(self.skipped)
            if index < self.cursor
        ]
        context = {
            "task": self.plan.task,
            "completed_steps": completed_steps,
            "skipped_steps": skipped_steps,
            "failed_step": failure.call.to_dict(),
            "error_code": failure.error_code,
            "error_message": failure.message[:2000],
            "recovery_assessment": assessment,
            "model_state_before": before_snapshot,
            "current_model_state": snapshot,
            "previous_failed_attempts": self.failure_history[-5:],
            "coordinate_contract": {
                "front": "local(x,y) -> world(X,Y)",
                "top": "local(x,y) -> world(X,-Z)",
                "right": "local(x,y) -> world(Y,Z)",
            },
            "available_tools": [item["name"] for item in self.registry.describe()],
            "original_remaining_steps": [
                call.to_dict() for call in self.plan.steps[self.cursor + 1 :]
            ],
        }
        return _redact_recovery_value(context)

    def skip_failed(self) -> None:
        failure = self._failure()
        if self.plan is None:
            raise ToolError("没有可跳过的计划步骤。")
        effective = [
            step
            for index, step in enumerate(self.plan.steps)
            if index != self.cursor and index not in self.skipped
        ]
        try:
            self.registry.validate_plan(effective)
        except Exception as exc:
            raise ToolError(
                "该步骤是后续建模的必要依赖，不能直接跳过；"
                "请修改参数、人工完成并验证，或让模型重建剩余步骤。"
            ) from exc
        self._archive_failure(failure, "skip")
        self.results.pop()
        self.skipped.add(self.cursor)
        index = self.cursor
        self.cursor += 1
        self.state = ExecutionState.READY
        self.audit.write("step_skipped", plan_id=self.plan.id if self.plan else "", index=index)
        self._emit("step_skipped", index=index)
        if self.plan is not None and self.cursor >= len(self.plan.steps):
            self._complete()

    def verify_manual_completion(self) -> bool:
        failure = self._failure()
        inspection = self.worker.call(ToolCall("model.inspect", {}), 60)
        postcondition = self.registry.postcondition(failure.call.tool)
        if not inspection.ok or postcondition is None:
            self._emit(
                "manual_verification_failed",
                index=self.cursor,
                message=inspection.message if not inspection.ok else "当前工具没有人工后验规则。",
            )
            return False
        ok, message = postcondition(
            failure.call,
            {"manual_verification": True},
            failure.before,
            inspection.after,
        )
        if not ok:
            self._emit("manual_verification_failed", index=self.cursor, message=message)
            return False
        self.results[-1].ok = True
        self.results[-1].message = "人工操作已通过后验验证。"
        self.results[-1].error_code = ""
        index = self.cursor
        self.cursor += 1
        self.state = ExecutionState.READY
        self.audit.write("manual_completion_verified", index=index)
        self._emit("manual_completion_verified", index=index)
        if self.plan is not None and self.cursor >= len(self.plan.steps):
            self._complete()
        return True

    def abort(self) -> None:
        self._abort = True
        if self.state != ExecutionState.RUNNING:
            self.state = ExecutionState.ABORTED
        self.audit.write("abort_requested", cursor=self.cursor)
        self._emit("abort_requested", cursor=self.cursor)

    def reset(self) -> None:
        self.plan = None
        self.cursor = 0
        self.results.clear()
        self.skipped.clear()
        self.failure_history.clear()
        self._abort = False
        self.state = ExecutionState.IDLE
        self._emit("session_reset")

    def _failure(self) -> StepResult:
        if self.state != ExecutionState.PAUSED or not self.results or self.results[-1].ok:
            raise ToolError("当前没有等待恢复的失败步骤。")
        return self.results[-1]

    def recovery_assessment(self) -> dict[str, Any]:
        """Return an explainable policy decision for the current failure."""

        return assess_failure(self._failure()).to_dict()

    def _archive_failure(self, result: StepResult, action: str) -> None:
        assessment = assess_failure(result).to_dict()
        self.failure_history.append(
            {
                "action": action,
                "failed_step": result.call.to_dict(),
                "error_code": result.error_code,
                "error_message": result.message[:1000],
                "assessment": assessment,
                "model_state_after": (
                    result.after.to_dict() if result.after is not None else None
                ),
            }
        )
        if len(self.failure_history) > 20:
            del self.failure_history[:-20]

    @staticmethod
    def _fingerprint(result: StepResult) -> str:
        context = {}
        if result.after is not None:
            context = {
                "document_title": result.after.document_title,
                "document_type": result.after.document_type,
                "active_sketch": result.after.active_sketch,
                "sketch_entity_count": result.after.sketch_entity_count,
                "feature_count": result.after.feature_count,
                "body_count": result.after.body_count,
                "volume_m3": result.after.volume_m3,
                "selection_count": result.after.selection_count,
            }
        return "|".join(
            (
                result.call.tool,
                json.dumps(result.call.arguments, ensure_ascii=False, sort_keys=True),
                result.error_code,
                result.message,
                json.dumps(context, ensure_ascii=False, sort_keys=True),
            )
        )

    @staticmethod
    def _model_changed_during_failure(result: StepResult) -> bool:
        before = result.before
        after = result.after
        if before is None or after is None:
            return False
        return any(
            (
                before.document_title != after.document_title,
                before.document_path != after.document_path,
                before.document_type != after.document_type,
                before.active_sketch != after.active_sketch,
                before.sketch_entity_count != after.sketch_entity_count,
                before.feature_count != after.feature_count,
                before.body_count != after.body_count,
                not math.isclose(before.volume_m3, after.volume_m3, rel_tol=1e-9, abs_tol=1e-15),
            )
        )

    def _complete(self) -> None:
        self.state = ExecutionState.COMPLETED
        self.audit.write(
            "plan_completed",
            plan_id=self.plan.id if self.plan else "",
            skipped_count=len(self.skipped),
        )
        self._emit("plan_completed", skipped_count=len(self.skipped))

    def _emit(self, name: str, **payload: Any) -> None:
        self.on_event(name, payload)
