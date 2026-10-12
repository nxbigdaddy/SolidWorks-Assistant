from __future__ import annotations

import re
from typing import Any

from ..domain import ClarificationRequired, Plan, PlanningError, ToolCall
from ..tools import ToolRegistry
from .models import PlanningAction, PlanningDecision


_EXPLICIT_MEASUREMENT = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[x×*]\s*\d+(?:\.\d+)?"
    r"(?:\s*[x×*]\s*\d+(?:\.\d+)?)?|"
    r"\d+(?:\.\d+)?\s*(?:毫米|mm|厘米|cm|米|m|度|°)\b|"
    r"(?:半径|直径|高度|深度|长度|宽度|厚度|拉伸|切除)[^\d]{0,12}\d+(?:\.\d+)?)",
    re.I,
)


class PlanningAgentPolicy:
    """Deterministic authorization for initial plans produced by a model."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def authorize(self, decision: PlanningDecision, prompt: str) -> Plan:
        if decision.action == PlanningAction.REQUEST_CLARIFICATION:
            raise ClarificationRequired(decision.questions)
        if decision.action == PlanningAction.UNSUPPORTED:
            raise PlanningError("需求规划 Agent 无法在当前工具能力内安全建模：" + decision.reason)

        calls = [
            ToolCall.from_mapping(step.model_dump(mode="python"))
            for step in decision.steps
        ]
        validated = self.registry.validate_plan(calls)
        if self._contains_numeric_geometry(validated):
            if not _EXPLICIT_MEASUREMENT.search(prompt) and not decision.assumptions:
                raise PlanningError(
                    "Agent 使用了用户未提供的尺寸，但没有在 assumptions 中声明。"
                )
        return Plan(
            decision.task.strip(),
            validated,
            assumptions=list(decision.assumptions),
        )

    @classmethod
    def validate_for_retry(cls, decision: PlanningDecision, prompt: str, registry: ToolRegistry) -> None:
        cls(registry).authorize(decision, prompt)

    @staticmethod
    def _contains_numeric_geometry(calls: list[ToolCall]) -> bool:
        ignored_tools = {"session.ensure", "document.new_part", "document.open_readonly"}

        def contains(value: Any) -> bool:
            if isinstance(value, bool) or value is None:
                return False
            if isinstance(value, (int, float)):
                return True
            if isinstance(value, list):
                return any(contains(item) for item in value)
            if isinstance(value, dict):
                return any(contains(item) for item in value.values())
            return False

        return any(call.tool not in ignored_tools and contains(call.arguments) for call in calls)
