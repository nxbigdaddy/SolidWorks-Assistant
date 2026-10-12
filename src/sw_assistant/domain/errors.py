class AssistantError(RuntimeError):
    """Base error with a message suitable for the desktop UI."""


class ConfigurationError(AssistantError):
    pass


class PlanningError(AssistantError):
    """Planning failure with optional JSON-safe diagnostic context."""

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        self.details = dict(details or {})
        super().__init__(message)


class ClarificationRequired(PlanningError):
    def __init__(self, questions: list[str]) -> None:
        self.questions = [
            str(question).strip()
            for question in questions
            if str(question).strip()
        ]
        super().__init__("需要补充建模条件：" + "；".join(self.questions))


class ToolError(AssistantError):
    pass


class WorkerError(AssistantError):
    pass
