from __future__ import annotations

import multiprocessing
import copy
import queue
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, Callable, Protocol

from ..domain import StepResult, ToolCall
from ..domain.errors import WorkerError
from .checkpoint_store import validate_checkpoint, validate_task
from .worker import worker_main


_PROGRESS_PHASES = {
    "waiting_gate", "before_observation", "precondition", "executing", "after_observation",
    "begin_candidate", "saving_checkpoint", "restoring_candidate", "listing_checkpoints",
    "restoring_checkpoint",
}
_CANDIDATE_TIMEOUTS = {"begin": 180, "finish": 240}
_CHECKPOINT_TIMEOUTS = {"list": 120, "restore": 240}


class ToolWorker(Protocol):
    def call(self, call: ToolCall, timeout_seconds: int = 120) -> StepResult: ...

    def close(self) -> None: ...


class SolidWorksWorkerClient:
    def __init__(self, candidate_workspace: Path | None = None, *,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None,
                 allow_start_solidworks: bool = True) -> None:
        if type(allow_start_solidworks) is not bool:
            raise ValueError("allow_start_solidworks must be a host boolean")
        self._allow_start_solidworks = allow_start_solidworks
        self._candidate_workspace = str(candidate_workspace.resolve()) if candidate_workspace else None
        self._context = multiprocessing.get_context("spawn")
        self._requests: Any | None = None
        self._responses: Any | None = None
        self._process: Any | None = None
        self._call_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._operation_local = threading.local()
        self._on_progress = on_progress

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Bind this logical operation to the current worker cancellation epoch."""
        previous = getattr(self._operation_local, "generation", None)
        with self._state_lock:
            # A nested scope must not launder an outer cancellation into a fresh
            # epoch (for example between completion and checkpoint acceptance).
            self._operation_local.generation = self._generation if previous is None else previous
        try:
            yield
        finally:
            self._operation_local.generation = previous

    @property
    def alive(self) -> bool:
        with self._state_lock:
            return bool(self._process and self._process.is_alive())

    def start(self) -> None:
        with self._state_lock:
            if self._process and self._process.is_alive():
                return
            self._requests = self._context.Queue()
            self._responses = self._context.Queue()
            process = self._context.Process(
                target=worker_main,
                args=(self._requests, self._responses, self._candidate_workspace, self._allow_start_solidworks),
                name="SolidWorksCOMWorker",
                daemon=True,
            )
            process.start()
            self._process = process

    def call(self, call: ToolCall, timeout_seconds: int = 120) -> StepResult:
        return self._call(call, timeout_seconds)

    def call_checked(self, call: ToolCall, expected: dict[str, Any], timeout_seconds: int = 120) -> StepResult:
        """Send the host's verified observation outside model-controlled ToolCall arguments."""
        if not isinstance(expected, dict) or expected.get("complete") is not True:
            raise WorkerError("写入前需要完整的宿主观察结果，未执行命令。")
        return self._call(call, timeout_seconds, expected=copy.deepcopy(expected))

    def _call(self, call: ToolCall, timeout_seconds: int, *, expected: dict[str, Any] | None = None) -> StepResult:
        request = {"kind": "call", "call": call.to_dict()}
        if expected is not None:
            request.update(kind="checked_call", expected=expected)

        def decode(response: dict[str, Any]) -> StepResult:
            if response.get("kind") not in {None, "call", "checked_call"} or not isinstance(response.get("result"), dict):
                raise WorkerError("SolidWorks 返回了无效的工具执行结果。")
            return StepResult.from_mapping(response["result"])

        return self._request(request, max(1, int(timeout_seconds)), decode, tool=call.tool)

    def probe_document_identity(self, timeout_seconds: int = 30) -> dict[str, Any]:
        """Read only the current document header through a private host RPC.

        Starting the helper does not connect to or start SolidWorks. This is not
        an Agent tool and never replaces a modeling call's state verification.
        """
        timeout = min(120, max(1, int(timeout_seconds)))

        def decode(response: dict[str, Any]) -> dict[str, Any]:
            if response.get("kind") != "identity" or response.get("ok") is not True:
                raise WorkerError(str(response.get("error") or "无法可靠读取当前文档身份。"))
            return _validated_document_identity(response.get("identity"))

        return self._request({"kind": "identity", "timeout_seconds": timeout}, timeout, decode,
                             operation="identity")

    @property
    def candidate_workspace_enabled(self) -> bool:
        return self._candidate_workspace is not None

    def candidate_request(self, operation: str, **payload: Any) -> dict[str, Any]:
        """Private bounded host RPC. The model cannot choose paths or invoke it."""
        if not self.candidate_workspace_enabled or not isinstance(operation, str) or operation not in {"begin", "finish"}:
            raise WorkerError("独立副本试验未启用或请求无效。")
        allowed = {"expected", "task_id"} if operation == "begin" else {"token", "accept", "expected"}
        if set(payload) != allowed:
            raise WorkerError("副本试验请求字段无效。")
        if operation == "finish" and not isinstance(payload["accept"], bool):
            raise WorkerError("accept 必须是布尔值。")
        return self._request({"kind": "candidate", "operation": operation, **copy.deepcopy(payload)},
                             _CANDIDATE_TIMEOUTS[operation], self._private_decoder("candidate"), operation=operation)

    def checkpoint_request(self, operation: str, task_id: str,
                           checkpoint_id: str | None = None) -> dict[str, Any]:
        """Host-only checkpoint RPC. Models cannot provide paths or deadlines."""
        if not self.candidate_workspace_enabled or not isinstance(operation, str) or operation not in _CHECKPOINT_TIMEOUTS:
            raise WorkerError("检查点工作区未启用或请求无效。")
        try:
            validate_task(task_id)
            if operation == "list":
                if checkpoint_id is not None:
                    raise ValueError("list does not accept checkpoint_id")
            else:
                if not isinstance(checkpoint_id, str):
                    raise ValueError("restore requires checkpoint_id")
                validate_checkpoint(checkpoint_id)
        except (ValueError, TypeError) as exc:
            raise WorkerError("检查点请求字段无效。") from exc
        request: dict[str, Any] = {"kind": "checkpoint", "operation": operation, "task_id": task_id}
        if operation == "restore":
            request["checkpoint_id"] = checkpoint_id
        return self._request(request, _CHECKPOINT_TIMEOUTS[operation], self._private_decoder("checkpoint"),
                             operation=f"checkpoint.{operation}")

    @staticmethod
    def _private_decoder(kind: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def decode(response: dict[str, Any]) -> dict[str, Any]:
            if (response.get("kind") != kind or response.get("ok") is not True
                    or not isinstance(response.get("data"), dict)):
                raise WorkerError(str(response.get("error") or "SolidWorks 私有操作返回无效结果。"))
            return response["data"]
        return decode

    def _check_generation(self, generation: int, *, submitted: bool = False) -> None:
        with self._state_lock:
            operation = getattr(self._operation_local, "generation", None)
            if generation != self._generation or operation is not None and operation != self._generation:
                if submitted:
                    raise WorkerError("SolidWorks 操作已停止，已丢弃晚到结果。")
                raise WorkerError("该操作已被停止，不能重新启动 SolidWorks 工作进程。")

    def _emit_progress(self, event: str, request_id: str, started: float,
                       phase: str, *, operation: str, tool: str) -> None:
        callback = self._on_progress
        if callback is None:
            return
        details = {"request_id": request_id, "phase": phase,
                   "elapsed_seconds": round(max(0, time.monotonic() - started), 3)}
        if operation:
            details["operation"] = operation
        if tool:
            details["tool"] = tool
        try:
            callback(event, details)
        except Exception:
            pass  # UI/telemetry errors must never change CAD execution semantics.

    @contextmanager
    def _serialized_request(self, generation: int, remaining: Callable[[], float],
                            waiting: Callable[[str], None]) -> Iterator[None]:
        # Real locks are interruptibly bounded. Context-manager-only injected
        # locks remain supported for existing deterministic cancellation tests.
        acquire = getattr(self._call_lock, "acquire", None)
        if not callable(acquire):
            with self._call_lock:
                self._check_generation(generation)
                remaining()
                yield
            return
        while True:
            self._check_generation(generation)
            if acquire(timeout=min(0.25, remaining())):
                break
            waiting("waiting_client_lock")
        try:
            self._check_generation(generation)
            remaining()
            yield
        finally:
            self._call_lock.release()

    def _request(self, request: dict[str, Any], timeout: int,
                 decode: Callable[[dict[str, Any]], Any], *, operation: str = "", tool: str = "") -> Any:
        """One enqueue, one fixed deadline including client/worker queue waits.

        Progress is observational only: neither native phases nor heartbeats
        extend deadlines. Timeout after submission has an unknown outcome and
        interrupts the helper; cancellation epochs prevent a silent replay.
        """
        started = time.monotonic()
        deadline, next_waiting = started + timeout, started + 5
        request_id = uuid.uuid4().hex
        request = {**request, "id": request_id}
        if self._on_progress is not None:
            request["progress"] = True
        with self._state_lock:
            generation = self._generation
        submitted = False
        outcome = "failed"
        last_phase = "waiting_worker"

        def emit(event: str, phase: str) -> None:
            self._emit_progress(event, request_id, started, phase, operation=operation, tool=tool)

        def remaining() -> float:
            nonlocal outcome
            value = deadline - time.monotonic()
            if value <= 0:
                outcome = "timed_out_unknown" if submitted else "not_executed"
                if submitted:
                    self.interrupt()
                suffix = "结果未知，需检查现场；未重放请求。" if submitted else "尚未提交，未执行命令。"
                raise WorkerError(f"SolidWorks 工作进程在 {timeout} 秒内没有返回，{suffix}")
            return value

        def waiting(phase: str) -> None:
            nonlocal next_waiting
            now = time.monotonic()
            if now >= next_waiting:
                emit("rpc.waiting", phase)
                next_waiting = now + 5

        emit("rpc.started", "waiting_client_lock")
        try:
            with self._serialized_request(generation, remaining, waiting):
                with self._state_lock:
                    self._check_generation(generation)
                    remaining()
                    self.start()
                    requests, responses, process = self._requests, self._responses, self._process
                    if requests is None or responses is None or process is None:
                        raise WorkerError("SolidWorks 工作进程没有启动。")
                    remaining()
                    self._check_generation(generation)
                    submitted = True
                    requests.put(request)
                while True:
                    self._check_generation(generation, submitted=True)
                    left = remaining()
                    if not process.is_alive():
                        outcome = "submitted_unknown"
                        self.interrupt()  # Fence this operation before any caller attempts another RPC.
                        raise WorkerError("SolidWorks 自动化调用已停止或工作进程意外退出。")
                    waiting(last_phase)
                    try:
                        response = responses.get(timeout=min(0.25, left))
                    except queue.Empty:
                        continue
                    self._check_generation(generation, submitted=True)
                    remaining()  # Late responses cannot turn an expired request into success.
                    if not isinstance(response, dict) or response.get("id") != request_id:
                        continue
                    if response.get("kind") == "progress":
                        phase = response.get("phase")
                        if isinstance(phase, str) and phase in _PROGRESS_PHASES:
                            last_phase = phase
                            emit("rpc.phase", phase)
                        continue
                    result = decode(response)
                    with self._state_lock:
                        # Finalization is one cancellation linearization point.
                        # Check generation after the deadline read as reentrant
                        # local callbacks can cancel even under this RLock.
                        remaining()
                        self._check_generation(generation, submitted=True)
                        outcome = "completed"
                        return result
        except Exception:
            if outcome == "failed":
                with self._state_lock:
                    stopped = generation != self._generation
                outcome = ("stopped_unknown" if stopped else "submitted_unknown") if submitted else "not_executed"
            raise
        finally:
            emit("rpc.finished", outcome)

    def interrupt(self) -> None:
        """Interrupt an active COM call without terminating SolidWorks itself."""

        with self._state_lock:
            self._generation += 1
            process = self._process
            self._process = None
            self._requests = None
            self._responses = None
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=3)

    def disconnect(self) -> None:
        """Stop the helper process without terminating the SolidWorks application."""

        with self._state_lock:
            self._generation += 1
            process = self._process
            requests = self._requests
            self._process = None
            self._requests = None
            self._responses = None
        if process is None:
            return
        if requests is not None and process.is_alive():
            requests.put({"kind": "shutdown"})
            process.join(timeout=3)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)

    def close(self) -> None:
        self.disconnect()


def _validated_document_identity(value: Any) -> dict[str, Any]:
    """Do not interpret missing/unknown identity evidence as an empty document."""
    fields = ("connected", "document_identity", "document_title", "document_path", "document_type")
    if not isinstance(value, dict) or any(key not in value for key in fields):
        raise WorkerError("文档身份检查返回了不完整结果。")
    connected, identity, title, path, kind = (value[key] for key in fields)
    if (not isinstance(connected, bool) or not isinstance(title, str)
            or not isinstance(path, str) or not isinstance(kind, str)
            or len(title) > 1024 or len(path) > 32768 or "\0" in title or "\0" in path):
        raise WorkerError("文档身份检查返回了无效结果。")
    if kind == "none":
        valid = identity is None and title == "" and path == ""
    else:
        valid = (connected and kind in {"part", "assembly", "drawing"}
                 and isinstance(identity, str) and 1 <= len(identity) <= 256
                 and "\0" not in identity and bool(title))
    if not valid:
        raise WorkerError("当前文档身份未知或不一致，不能继续操作。")
    return {key: value[key] for key in fields}
