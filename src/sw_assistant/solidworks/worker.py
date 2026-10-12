from __future__ import annotations

import time
import traceback
from contextlib import nullcontext
from multiprocessing.queues import Queue

from ..domain import ModelSnapshot, StepResult, ToolCall
from .gateway import CircleEditError, PolygonCreationError, SketchCreationError, SolidWorksGateway
from .preconditions import StateChanged, validate_precondition
from .serial_gate import solidworks_serial_gate


def worker_main(requests: Queue, responses: Queue, candidate_workspace: str | None = None,
                allow_start_solidworks: bool = True) -> None:
    if type(allow_start_solidworks) is not bool:
        raise ValueError("allow_start_solidworks must be a host boolean")
    gateway = SolidWorksGateway(visible=True)
    gateway._allow_start = allow_start_solidworks
    if candidate_workspace is not None:
        gateway.configure_candidate_workspace(candidate_workspace)
    try:
        while True:
            request = requests.get()
            if request.get("kind") == "shutdown":
                break
            request_id = str(request.get("id"))
            started = time.monotonic()

            def progress(phase: str) -> None:
                # Opt-in, metadata only. Never expose model arguments or CAD data.
                if request.get("progress") is True:
                    responses.put({"id": request_id, "kind": "progress", "phase": phase})

            optional_keys = {"progress"} if "progress" in request else set()
            if request.get("kind") == "candidate":
                try:
                    operation = request.get("operation")
                    expected_keys = ({"expected", "task_id"} if operation == "begin"
                                     else {"expected", "token", "accept"})
                    if (operation not in {"begin", "finish"} or candidate_workspace is None
                            or set(request) != {"kind", "id", "operation"} | expected_keys | optional_keys
                            or not isinstance(request.get("progress", False), bool)):
                        raise ValueError("副本试验请求不被允许。")
                    progress("waiting_gate")
                    with solidworks_serial_gate(120):
                        if operation == "begin":
                            progress("begin_candidate")
                            data = gateway.begin_candidate(request["expected"], request["task_id"])
                        else:
                            if not isinstance(request["accept"], bool):
                                raise ValueError("accept 必须是布尔值。")
                            progress("saving_checkpoint" if request["accept"] else "restoring_candidate")
                            data = gateway.finish_candidate(request["token"], request["accept"], request["expected"])
                    response = {"id": request_id, "kind": "candidate", "ok": True, "data": data}
                except Exception as exc:
                    response = {"id": request_id, "kind": "candidate", "ok": False,
                                "error": str(exc), "error_code": type(exc).__name__}
                responses.put(response)
                continue
            if request.get("kind") == "checkpoint":
                try:
                    operation = request.get("operation")
                    expected_keys = {"kind", "id", "operation", "task_id"} | optional_keys
                    if operation == "restore":
                        expected_keys.add("checkpoint_id")
                    if (operation not in {"list", "restore"} or candidate_workspace is None
                            or set(request) != expected_keys
                            or not isinstance(request.get("progress", False), bool)):
                        raise ValueError("检查点请求不被允许。")
                    progress("waiting_gate")
                    with solidworks_serial_gate(120):
                        progress("listing_checkpoints" if operation == "list" else "restoring_checkpoint")
                        data = gateway.checkpoint_request(operation, request["task_id"], request.get("checkpoint_id"))
                    response = {"id": request_id, "kind": "checkpoint", "ok": True, "data": data}
                except Exception as exc:
                    response = {"id": request_id, "kind": "checkpoint", "ok": False,
                                "error": str(exc), "error_code": type(exc).__name__}
                responses.put(response)
                continue
            if request.get("kind") == "identity":
                # Host-only header read: no ToolCall, broad snapshot, or model
                # execution. The same process-wide gate still serializes COM.
                try:
                    timeout = min(120, max(1, int(request.get("timeout_seconds", 30))))
                    with solidworks_serial_gate(timeout):
                        identity = gateway.probe_document_identity()
                    response = {"id": request_id, "kind": "identity", "ok": True, "identity": identity}
                except Exception as exc:
                    response = {"id": request_id, "kind": "identity", "ok": False,
                                "error": str(exc), "error_code": type(exc).__name__}
                response["duration_ms"] = int((time.monotonic() - started) * 1000)
                responses.put(response)
                continue
            call = ToolCall("invalid.request", {})
            before = None
            checked = request.get("kind") == "checked_call"
            try:
                if request.get("kind") not in (None, "call", "checked_call"):
                    raise StateChanged("未知执行请求类型，拒绝降级为普通执行。", ["request.kind"])
                if not checked and "expected" in request:
                    raise StateChanged("预期状态只能通过 checked_call 进行验证执行。", ["request.kind"])
                if not isinstance(request.get("progress", False), bool):
                    raise StateChanged("执行进度请求字段无效。", ["request.progress"])
                if checked and set(request) != {"kind", "id", "call", "expected"} | optional_keys:
                    raise StateChanged("执行前检查请求字段缺失或包含未知字段。", ["request"])
                call = ToolCall.from_mapping(request.get("call", {}))
            except Exception as exc:
                result = StepResult(
                    False,
                    call,
                    str(exc),
                    error_code=type(exc).__name__,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    data={"traceback": traceback.format_exc(limit=8),
                          **({"execution": {"started": False, "phase": "precondition"},
                              "precondition": exc.evidence} if isinstance(exc, StateChanged) else {})},
                )
            else:
                execution_started = False
                try:
                    progress("waiting_gate")
                    with solidworks_serial_gate(120):
                        try:
                            progress("before_observation")
                            # Strict observe already reads and validates a whole
                            # observation. Two extra best-effort traversals add
                            # latency and can describe different document states.
                            before = None if call.tool == "model.observe" and not checked else gateway.snapshot()
                        except Exception:
                            # Focused read-only diagnostics must remain usable
                            # when the broad snapshot is exactly what failed.
                            if checked:
                                raise StateChanged("执行前检查无法读取当前模型快照。", ["before"])
                            if call.tool not in {
                                "model.inspect", "model.observe", "session.capabilities",
                                "model.references", "sketch.inspect", "feature.inspect", "model.measure_extents",
                            }:
                                raise
                            before = None
                        if checked:
                            progress("precondition")
                            validate_precondition(gateway, request["expected"], before)
                        bind = getattr(gateway, "execution_document", None)
                        scope = bind(request["expected"]) if checked and callable(bind) else nullcontext()
                        try:
                            with scope:
                                progress("executing")
                                execution_started = True
                                message, data = gateway.execute(call)
                            progress("after_observation")
                            try:
                                if call.tool == "model.observe" and not checked:
                                    value = data.get("snapshot") if data.get("complete") is True else None
                                    after = ModelSnapshot.from_mapping(value) if isinstance(value, dict) else None
                                    before = after
                                else:
                                    after = gateway.snapshot()
                            except Exception:
                                after = None
                            result = StepResult(
                                True,
                                call,
                                message,
                                data=data,
                                before=before,
                                after=after,
                                duration_ms=int((time.monotonic() - started) * 1000),
                            )
                        except Exception as exc:
                            progress("after_observation")
                            try:
                                after = None if call.tool == "model.observe" and not checked else gateway.snapshot()
                            except Exception:
                                after = None
                            result = StepResult(
                                False,
                                call,
                                str(exc),
                                error_code=type(exc).__name__,
                                before=before,
                                after=after,
                                duration_ms=int((time.monotonic() - started) * 1000),
                                data={"traceback": traceback.format_exc(limit=8),
                                      **({"execution": {"started": execution_started,
                                                         "phase": "executing" if execution_started else "precondition"},
                                          "precondition": exc.evidence} if isinstance(exc, StateChanged) else {}),
                                      **({"circle_edit": exc.evidence} if isinstance(exc, CircleEditError) else {}),
                                      **({"mutation": exc.evidence}
                                         if isinstance(exc, (PolygonCreationError, SketchCreationError)) else {})},
                            )
                except Exception as exc:
                    result = StepResult(
                        False,
                        call,
                        str(exc),
                        error_code=type(exc).__name__,
                        before=before,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        data={"traceback": traceback.format_exc(limit=8),
                              **({"execution": {"started": False, "phase": "precondition"},
                                  "precondition": exc.evidence} if isinstance(exc, StateChanged) else {})},
                    )
            responses.put({"id": request_id, "result": result.to_dict()})
    finally:
        gateway.release(close_owned=True)
