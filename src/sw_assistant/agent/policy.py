from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..domain import Plan, PlanningError, ToolCall
from ..tools import ToolRegistry
from .models import SupervisorAction, SupervisorDecision


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    decision: SupervisorDecision
    plan: Plan | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.model_dump(mode="json"),
            "plan": self.plan.to_dict() if self.plan is not None else None,
        }


class SupervisorPolicy:
    """Deterministic authorization boundary around model-produced decisions."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def authorize(
        self,
        decision: SupervisorDecision,
        context: dict[str, Any],
    ) -> SupervisorOutcome:
        assessment = context.get("recovery_assessment")
        if not isinstance(assessment, dict):
            raise PlanningError("监督 Agent 缺少结构化恢复判断，禁止应用建议。")

        if decision.action != SupervisorAction.REPLACE_REMAINING:
            return SupervisorOutcome(decision, None)

        if bool(assessment.get("model_changed")) or not bool(
            assessment.get("state_known")
        ):
            raise PlanningError("模型已经变化或状态未知，监督 Agent 不得替换后续步骤。")
        if not bool(assessment.get("ai_repair_recommended")):
            raise PlanningError("当前故障策略未授权监督 Agent 生成可执行替代步骤。")

        calls = [
            ToolCall.from_mapping(step.model_dump(mode="python"))
            for step in decision.replacement_steps
        ]
        self._reject_exact_failed_calls(calls, context)

        prefix_values = context.get("completed_steps", [])
        if not isinstance(prefix_values, list):
            raise PlanningError("监督 Agent 上下文中的已完成步骤格式无效。")
        prefix = [ToolCall.from_mapping(value) for value in prefix_values]
        combined = self.registry.validate_plan(prefix + calls)
        validated = combined[len(prefix) :]
        if not validated:
            raise PlanningError("监督 Agent 的替代计划通过校验后为空。")
        task = "监督 Agent 修复：" + decision.reason.strip()[:500]
        return SupervisorOutcome(decision, Plan(task, validated))

    @staticmethod
    def _reject_exact_failed_calls(
        calls: list[ToolCall],
        context: dict[str, Any],
    ) -> None:
        failed: set[str] = set()

        def add(value: Any) -> None:
            if not isinstance(value, dict):
                return
            tool = str(value.get("tool") or value.get("name") or "").strip()
            arguments = value.get("arguments")
            if tool and isinstance(arguments, dict):
                failed.add(
                    tool
                    + "|"
                    + json.dumps(
                        arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )

        add(context.get("failed_step"))
        for attempt in context.get("previous_failed_attempts", []):
            if isinstance(attempt, dict):
                add(attempt.get("failed_step"))

        for call in calls:
            signature = call.tool + "|" + json.dumps(
                call.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if signature in failed:
                raise PlanningError(
                    f"监督 Agent 重复了已经失败的步骤：{call.tool}，已拒绝应用。"
                )
