"""Strict proposals for the continuous, locally verified modelling loop."""

from __future__ import annotations

import math
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ProposedToolCall


class GoalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    metric: Literal[
        "body_count", "volume_mm3", "feature_count", "document_type",
        "dimension_mm", "extent_mm", "feature_present", "feature_errors",
    ]
    target: str = Field(default="", max_length=250)
    operator: Literal["eq", "ge", "le"] = "eq"
    expected: str | float | bool
    tolerance: float = Field(default=0.001, ge=0, allow_inf_nan=False)
    binding: Literal["fixed", "pending"] = "fixed"

    @model_validator(mode="after")
    def validate_measure(self) -> "GoalCheck":
        if self.metric == "extent_mm" and (self.target not in {"x", "y", "z"} or self.binding != "fixed"):
            raise ValueError("extent_mm requires fixed binding and target x / y / z in model coordinates")
        if self.metric in {"dimension_mm", "feature_present"} and self.binding == "fixed" and not self.target.strip():
            raise ValueError("dimension_mm / feature_present require an exact target")
        if self.binding == "pending" and self.metric not in {"dimension_mm", "feature_present"}:
            raise ValueError("only named dimensions / features can have pending bindings")
        if self.metric == "document_type":
            if self.operator != "eq" or self.expected not in {"part", "assembly", "drawing"}:
                raise ValueError("document_type requires eq and a known document type")
        elif self.metric == "feature_present":
            if self.operator != "eq" or not isinstance(self.expected, bool):
                raise ValueError("feature_present requires eq and a boolean")
        elif isinstance(self.expected, (str, bool)) or not math.isfinite(self.expected):
            raise ValueError("numeric metrics require a finite numeric expected value")
        return self


class RequirementCheck(BaseModel):
    """A durable user requirement or an agent-proposed child with goal references."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    source_message_id: str = Field(default="", max_length=80)
    source_text: str = Field(default="", max_length=4000)
    parent_id: str = Field(default="", max_length=80)
    goal_ids: list[str] = Field(default_factory=list, max_length=32)
    source_kind: Literal["requirement", "control"] = "requirement"


class TrialSpec(BaseModel):
    """A replaceable program candidate, not a change to the final requirements."""

    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=1, max_length=2000)
    checks: list[GoalCheck] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_checks(self) -> "TrialSpec":
        if any(check.binding != "fixed" for check in self.checks):
            raise ValueError("trial checks must be immediately measurable, not pending")
        if len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("trial check ids must be unique")
        if not any(check.metric in {"volume_mm3", "extent_mm", "dimension_mm", "feature_present"}
                   for check in self.checks):
            raise ValueError("trial requires a geometric check, not only document health or body count")
        return self


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["act", "inspect", "review", "complete", "stop"]
    reason: str = Field(min_length=1, max_length=4000)
    phase: str = Field(default="", max_length=300)
    steps: list[ProposedToolCall] = Field(default_factory=list, max_length=12)
    goals: list[GoalCheck] = Field(default_factory=list, max_length=32)
    assumptions: list[str] = Field(default_factory=list, max_length=32)
    questions: list[str] = Field(default_factory=list, max_length=3)
    requirements: list[RequirementCheck] = Field(default_factory=list, max_length=64)
    review_kind: Literal["clarification", "capability", "safety", "user_decision"] = "user_decision"
    review_critical: bool = False
    trial: TrialSpec | None = None

    @model_validator(mode="after")
    def validate_action(self) -> "TaskDecision":
        if self.trial is not None and self.action != "act":
            raise ValueError("trial is only valid for act")
        if self.action == "act" and not self.steps:
            raise ValueError("act requires steps")
        if self.action not in {"act", "inspect"} and self.steps:
            raise ValueError("review / complete / stop cannot carry executable steps")
        if len({goal.id for goal in self.goals}) != len(self.goals):
            raise ValueError("goal ids must be unique")
        if len({item.id for item in self.requirements}) != len(self.requirements):
            raise ValueError("requirement ids must be unique")
        return self


class TaskBackend(Protocol):
    def decide(self, context: dict[str, Any]) -> TaskDecision: ...

    def close(self) -> None: ...
