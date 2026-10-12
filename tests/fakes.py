from __future__ import annotations

from collections import deque

from sw_assistant.domain import ModelSnapshot, StepResult, ToolCall


class FakeWorker:
    def __init__(self, results=None):
        self.results = deque(results or [])
        self.calls: list[ToolCall] = []
        self.closed = False

    def call(self, call: ToolCall, timeout_seconds: int = 120) -> StepResult:
        self.calls.append(call)
        if self.results:
            result = self.results.popleft()
            result.call = call
            return result
        before = ModelSnapshot(connected=False)
        after = ModelSnapshot(connected=True)
        return StepResult(True, call, "ok", before=before, after=after)

    def close(self):
        self.closed = True


def result(
    ok=True,
    *,
    before: ModelSnapshot | None = None,
    after: ModelSnapshot | None = None,
    message="ok",
    data=None,
):
    return StepResult(
        ok,
        ToolCall("placeholder", {}),
        message,
        error_code="FakeError" if not ok else "",
        before=before,
        after=after,
        data=data or {},
    )
