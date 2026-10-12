from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SupervisorAction(StrEnum):
    REPLACE_REMAINING = "replace_remaining"
    REQUEST_REVIEW = "request_review"
    STOP = "stop"


class PlanningAction(StrEnum):
    CREATE_PLAN = "create_plan"
    REQUEST_CLARIFICATION = "request_clarification"
    UNSUPPORTED = "unsupported"


class ProposedToolCall(BaseModel):
    """A model proposal, not an authorization to execute SolidWorks."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(default="", max_length=500)


class SupervisorDecision(BaseModel):
    """The only output contract accepted from the embedded supervisor agent."""

    model_config = ConfigDict(extra="forbid")

    action: SupervisorAction
    reason: str = Field(min_length=1, max_length=2000)
    replacement_steps: list[ProposedToolCall] = Field(default_factory=list, max_length=64)
    expected_postconditions: list[str] = Field(default_factory=list, max_length=16)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "SupervisorDecision":
        if self.action == SupervisorAction.REPLACE_REMAINING:
            if not self.replacement_steps:
                raise ValueError("replace_remaining requires replacement_steps")
            if not self.expected_postconditions:
                raise ValueError("replace_remaining requires expected_postconditions")
        elif self.replacement_steps:
            raise ValueError("review/stop decisions cannot carry executable steps")
        return self


class PlanningDecision(BaseModel):
    """Structured initial-planning result proposed by the requirements agent."""

    model_config = ConfigDict(extra="forbid")

    action: PlanningAction
    task: str = Field(default="", max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=16)
    questions: list[str] = Field(default_factory=list, max_length=3)
    steps: list[ProposedToolCall] = Field(default_factory=list, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "PlanningDecision":
        self.assumptions = [item.strip() for item in self.assumptions if item.strip()]
        self.questions = [item.strip() for item in self.questions if item.strip()]
        if self.action == PlanningAction.CREATE_PLAN:
            if not self.task.strip():
                raise ValueError("create_plan requires task")
            if not self.steps:
                raise ValueError("create_plan requires steps")
            if self.questions:
                raise ValueError("create_plan cannot carry clarification questions")
        elif self.action == PlanningAction.REQUEST_CLARIFICATION:
            if not self.questions:
                raise ValueError("request_clarification requires questions")
            if self.steps:
                raise ValueError("clarification decisions cannot carry executable steps")
        elif self.steps or self.questions:
            raise ValueError("unsupported decisions cannot carry steps or questions")
        return self
