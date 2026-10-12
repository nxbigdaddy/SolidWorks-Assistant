from __future__ import annotations

import json
import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .time_budget import turn_budget_seconds


class PiAgentError(RuntimeError):
    """Pi RPC process failed or returned an invalid protocol response."""


class PiUnavailableError(PiAgentError):
    """No usable Pi runtime is installed."""


class PiResponseTimeout(PiAgentError):
    """No new useful response arrived within the inactivity budget."""


class PiTurnBudgetExceeded(PiAgentError):
    """An active turn exceeded its independent hard limit; not a runtime failure."""


class PiCompactionTimeout(PiTurnBudgetExceeded):
    """Context maintenance exceeded a fixed budget; never trigger fallback."""


COMPACTION_TIMEOUT_SECONDS = 300.0


class PiDecisionLoopExceeded(PiAgentError):
    """A bounded decision attempt failed; the outer agent may correct it."""

    retryable_decision = True

    def __init__(self, message: str, *, attempt_count: int, investigation_count: int) -> None:
        super().__init__(message)
        self.attempt_count = attempt_count
        self.investigation_count = investigation_count


class PiDecisionOutputTruncated(PiAgentError):
    """The model exhausted output tokens without a complete trusted decision."""

    retryable_decision = True

    def __init__(self, *, output_tokens: int | None = None) -> None:
        self.output_tokens = output_tokens
        count = f"（{output_tokens} tokens）" if output_tokens is not None else ""
        super().__init__(
            f"Pi 本次模型响应达到输出长度上限{count}，未返回完整阶段决策；这不是服务断线。"
            "请保留已成功的 CAD 步骤，不重复执行；根据已有证据直接提交简短的下一阶段 JSON。"
        )


@dataclass(frozen=True, slots=True)
class PiRuntime:
    node: Path
    cli: Path
    provider_extension: Path
    source: str


def _usable_file(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def discover_pi_runtime(
    provider_extension: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> PiRuntime:
    """Locate an isolated Pi CLI without importing Pi or another product's runtime."""

    values = environ if environ is not None else os.environ
    extension = _usable_file(
        provider_extension
        or Path(__file__).resolve().parent / "pi_assets" / "provider.mjs"
    )
    if extension is None:
        raise PiUnavailableError("Pi 的受限模型提供器文件缺失。")

    application_dir = Path(sys.executable).resolve().parent
    bundled_node = application_dir / "PiEngine" / "node.exe"
    bundled_cli = (
        application_dir
        / "PiEngine"
        / "pi-coding-agent"
        / "dist"
        / "bundle"
        / "cli.js"
    )
    candidates: list[tuple[Path | None, Path | None, str]] = [
        (_usable_file(bundled_node), _usable_file(bundled_cli), "bundled"),
        (
            _usable_file(values.get("SW_ASSISTANT_NODE")),
            _usable_file(values.get("SW_ASSISTANT_PI_CLI")),
            "configured",
        ),
    ]

    node_command = shutil.which("node.exe") or shutil.which("node")
    system_node = _usable_file(node_command)
    appdata = values.get("APPDATA")
    if appdata:
        npm_root = Path(appdata) / "npm" / "node_modules"
        for package in (
            "@earendil-works/pi-coding-agent",
            "@mariozechner/pi-coding-agent",
        ):
            candidates.append(
                (
                    system_node,
                    _usable_file(Path(npm_root) / package / "dist" / "bundle" / "cli.js"),
                    "system",
                )
            )

    for node, cli, source in candidates:
        if node is not None and cli is not None:
            return PiRuntime(node=node, cli=cli, provider_extension=extension, source=source)
    raise PiUnavailableError(
        "未找到 Pi 运行时。请安装 @earendil-works/pi-coding-agent，"
        "或设置 SW_ASSISTANT_NODE 与 SW_ASSISTANT_PI_CLI。"
    )


EventCallback = Callable[[str, dict[str, Any]], None]
Investigator = Callable[[str, dict[str, Any]], dict[str, Any]]
Operator = Callable[[dict[str, Any]], dict[str, Any]]


class PiRpcClient:
    """Single-flight Pi RPC; optional application-owned read-only investigation."""

    def __init__(
        self,
        runtime: PiRuntime,
        *,
        api_base: str,
        model: str,
        api_key: str,
        state_root: str | os.PathLike[str],
        session_name: str,
        session_id: str = "",
        timeout_seconds: int = 90,
        on_event: EventCallback | None = None,
        system_prompt: str | None = None,
        investigator: Investigator | None = None,
        operator: Operator | None = None,
    ) -> None:
        self.runtime = runtime
        self.api_base = api_base
        self.model = model
        self.api_key = api_key
        self.state_root = Path(state_root).resolve()
        self.session_name = session_name[:100] or "solidworks-recovery"
        self.session_id = session_id or (
            "sw-"
            + hashlib.sha256(
                (str(self.state_root) + "|" + self.session_name).encode("utf-8")
            ).hexdigest()[:24]
        )
        self.timeout_seconds = max(1, min(int(timeout_seconds), 300))
        self.max_turn_seconds = turn_budget_seconds(self.timeout_seconds)
        self.on_event = on_event
        self.investigator = investigator
        self.operator = operator
        self.stage_execution_allowed = False
        self.stage_attempt_limit = 2
        self._stage_bridge_id = "sw-model-stage-v1-" + uuid.uuid4().hex
        self._stage_count = 0
        self._stage_tool_attempts = 0
        self._stage_bridge_attempts = 0
        self._stage_elapsed = 0.0
        self._stage_epoch = ""
        self._stage_uncertain = False
        self._stage_replies: dict[str, dict[str, Any]] = {}
        self._bridge_id = "sw-investigate-v1-" + uuid.uuid4().hex
        self._tool_replies: dict[str, dict[str, Any]] = {}
        self._tool_activity = 0
        self._investigation_count = 0
        self._tool_attempt_count = 0
        self._bridge_attempt_count = 0
        self._decision_only = False
        self._handoff_requested = False
        self._decision_deadline: float | None = None
        self._compacting = False
        self._compaction_deadline: float | None = None
        self._last_public_progress = 0.0
        self.system_prompt = system_prompt or (
            "You are the constrained recovery reasoning component of a SolidWorks "
            "application. You have no tools and cannot execute or modify anything. "
            "Return only the JSON object requested by the application."
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._diagnostics: queue.Queue[str] = queue.Queue(maxsize=32)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._aborted = threading.Event()

    @property
    def command(self) -> list[str]:
        sessions = self.state_root / "sessions"
        return [
            str(self.runtime.node),
            str(self.runtime.cli),
            "--mode",
            "rpc",
            "--provider",
            "solidworks-assistant",
            "--model",
            self.model,
            "--thinking",
            "off",
            "--session-dir",
            str(sessions),
            "--session-id",
            self.session_id,
            "--name",
            self.session_name,
            "--no-tools",
            *(["--tools", ",".join(name for name, enabled in (
                ("sw_investigate", self.investigator is not None), ("sw_model_stage", self.operator is not None)
            ) if enabled)] if self.investigator is not None or self.operator is not None else []),
            "--no-extensions",
            "--extension",
            str(self.runtime.provider_extension),
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--system-prompt",
            self.system_prompt,
        ]

    @property
    def child_environment(self) -> dict[str, str]:
        from ..infrastructure import ModelSettings
        from .provider_compat import build_agent_model_settings

        config = self.state_root / "config"
        provider_body = build_agent_model_settings(ModelSettings(self.api_base, self.model, "")).get("extra_body")
        request_body = dict(provider_body) if isinstance(provider_body, Mapping) else {}
        sensitive_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in sensitive_markers)
        }
        environment.update(
            {
                "PI_CODING_AGENT_DIR": str(config),
                "PI_CODING_AGENT_SESSION_DIR": str(self.state_root / "sessions"),
                "PI_TELEMETRY": "0",
                "SW_ASSISTANT_PI_API_BASE": self.api_base,
                "SW_ASSISTANT_PI_MODEL": self.model,
                "SW_ASSISTANT_PI_API_KEY": self.api_key or "not-required",
                # Reuse the same endpoint-specific policy as PydanticAI. Pi's
                # --thinking off alone does not switch off provider defaults.
                "SW_ASSISTANT_PI_REQUEST_BODY": json.dumps(request_body, separators=(",", ":")),
            }
        )
        # No listening port or runtime-discovered extensions. Only our explicit
        # provider can request this narrowly scoped RPC sub-protocol.
        environment.pop("SW_ASSISTANT_PI_BRIDGE_ID", None)
        environment.pop("SW_ASSISTANT_PI_STAGE_BRIDGE_ID", None)
        if self.investigator is not None:
            environment["SW_ASSISTANT_PI_BRIDGE_ID"] = self._bridge_id
        if self.operator is not None:
            environment["SW_ASSISTANT_PI_STAGE_BRIDGE_ID"] = self._stage_bridge_id
        return environment

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self._aborted.is_set():
            raise PiAgentError("Pi 会话已永久停止。")
        if self.running:
            return
        self.close()
        runtime_dir = self.state_root / "runtime"
        for path in (runtime_dir, self.state_root / "sessions", self.state_root / "config"):
            path.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with self._lifecycle_lock:
                if self._aborted.is_set():
                    raise PiAgentError("Pi 会话已永久停止。")
                self._process = subprocess.Popen(
                    self.command,
                    cwd=runtime_dir,
                    env=self.child_environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=flags,
                )
                process = self._process
                # Readers from a terminated process retain their own queue;
                # late stdout can never settle a newly started model request.
                self._messages = queue.Queue()
                messages = self._messages
        except OSError as exc:
            self._process = None
            raise PiAgentError(f"无法启动 Pi RPC：{exc}") from exc
        assert process.stdout is not None
        assert process.stderr is not None
        threading.Thread(
            target=self._read_stdout, args=(process.stdout, messages), daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, args=(process.stderr,), daemon=True
        ).start()

    def prompt(self, message: str) -> str:
        with self._lock:
            self.start()
            self._tool_replies = {}
            self._investigation_count = 0
            self._tool_attempt_count = 0
            self._bridge_attempt_count = 0
            self._decision_only = False
            self._handoff_requested = False
            self._decision_deadline = None
            self._compacting = False
            self._compaction_deadline = None
            self._stage_count = 0
            self._stage_tool_attempts = 0
            self._stage_bridge_attempts = 0
            self._stage_elapsed = 0.0
            self._stage_epoch = uuid.uuid4().hex
            if self.operator is not None:
                try:
                    payload = json.loads(message)
                except ValueError:
                    payload = {"request": message}
                if not isinstance(payload, dict):
                    payload = {"request": message}
                payload["host_stage_protocol"] = {
                    "epoch": self._stage_epoch, "allowed": self._stage_available(),
                    "max_attempts": self.stage_attempt_limit, "stage_timeout_seconds": 600, "bridge_timeout_ms": 630_000,
                }
                message = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            # The preceding prompt has settled before the single-flight lock
            # releases. Discard leftover housekeeping from that completed turn.
            while True:
                try:
                    self._messages.get_nowait()
                except queue.Empty:
                    break
            request_id = self._send("prompt", message=message)
            accepted = False
            settled = False
            started = False
            deadline = time.monotonic() + self.timeout_seconds
            hard_deadline = time.monotonic() + self.max_turn_seconds
            while not (accepted and settled):
                item = self._receive_turn(deadline, hard_deadline)
                if self._compacting and (
                    item.get("type") == "tool_execution_start"
                    or (item.get("type") == "extension_ui_request"
                        and item.get("method") in {"input", "select", "confirm", "editor"})
                ):
                    self.close()
                    raise PiTurnBudgetExceeded("Pi 正在整理上下文却收到新的工具请求；已保留现场并停止本轮，未执行或重放工具。")
                self._check_assistant_completion(item)
                self._check_tool_loop(item)
                previous_activity = self._tool_activity
                previous_stage_elapsed = self._stage_elapsed
                if self._handle_tool_request(item):
                    hard_deadline += self._stage_elapsed - previous_stage_elapsed
                    if self._tool_activity != previous_activity:
                        # CAD investigation time is not model inactivity. Only
                        # a validated host callback counts, not arbitrary UI spam.
                        deadline = time.monotonic() + self.timeout_seconds
                    continue
                if item.get("type") == "response" and item.get("id") == request_id:
                    if not item.get("success"):
                        raise PiAgentError(self._response_error(item, "Pi 拒绝了恢复请求"))
                    if not accepted:
                        deadline = time.monotonic() + self.timeout_seconds
                    accepted = True
                elif item.get("type") == "agent_start":
                    if not started:
                        deadline = time.monotonic() + self.timeout_seconds
                    started = True
                elif item.get("type") == "agent_settled" and started:
                    settled = True
                    self._compacting = False
                elif item.get("type") == "compaction_start":
                    # Context maintenance is not model-response inactivity. A
                    # single fixed window per prompt cannot be extended by
                    # repeated start events, heartbeats or compaction tokens.
                    if self._compaction_deadline is None:
                        self._compaction_deadline = time.monotonic() + COMPACTION_TIMEOUT_SECONDS
                    self._compacting = True
                elif item.get("type") == "compaction_end" and self._compacting:
                    self._compacting = False
                    deadline = time.monotonic() + self.timeout_seconds
                elif started and self._useful_response_progress(item):
                    deadline = time.monotonic() + self.timeout_seconds
                self._emit_event(item)
            text_id = self._send("get_last_assistant_text")
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                item = self._receive_turn(deadline, hard_deadline)
                self._check_assistant_completion(item)
                self._check_tool_loop(item)
                if self._handle_tool_request(item):
                    continue
                self._emit_event(item)
                if item.get("type") != "response" or item.get("id") != text_id:
                    continue
                if not item.get("success"):
                    raise PiAgentError(self._response_error(item, "Pi 无法读取最后回复"))
                data = item.get("data")
                text = data.get("text") if isinstance(data, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise PiAgentError("Pi 没有返回可用的恢复决策。")
                return text

    def _receive_turn(self, idle_deadline: float, hard_deadline: float) -> dict[str, Any]:
        effective_hard = min(hard_deadline, self._decision_deadline or hard_deadline)
        response_deadline = (self._compaction_deadline
                             if self._compacting and self._compaction_deadline is not None else idle_deadline)
        try:
            return self._receive(min(response_deadline, effective_hard))
        except PiResponseTimeout as exc:
            if self._compacting:
                self.close()
                reason = ("本轮剩余活动/决策预算" if effective_hard <= response_deadline else "上下文整理的固定预算")
                raise PiCompactionTimeout(
                    f"Pi 上下文整理达到{reason}上限（整理最长 {COMPACTION_TIMEOUT_SECONDS:g} 秒），"
                    "已保留任务与检查点并暂停；这不是模型服务失联，未自动切换引擎或重放 CAD。"
                ) from exc
            if self._decision_only:
                self._raise_decision_loop("调查结束后仍未提交阶段决策，已停止本次候选，交回任务循环纠正。")
            if hard_deadline <= idle_deadline:
                raise PiTurnBudgetExceeded(
                    f"Pi 本轮达到 {self.max_turn_seconds:g} 秒活动预算，已保留任务并停止本轮；"
                    "这不是模型服务失联。"
                ) from exc
            raise

    def _raise_decision_loop(self, reason: str) -> None:
        # End this stream before a retry. Unlike user abort(), this does not
        # permanently cancel the client; a new prompt may reopen its session.
        self.close()
        raise PiDecisionLoopExceeded(
            reason, attempt_count=max(self._tool_attempt_count, self._bridge_attempt_count),
            investigation_count=self._investigation_count,
        )

    def _check_assistant_completion(self, item: dict[str, Any]) -> None:
        message = item.get("message")
        if item.get("type") != "message_end" or not isinstance(message, dict):
            return
        if message.get("role") != "assistant" or message.get("stopReason") != "length":
            return
        # A provider can return only reasoning tokens, even with Pi thinking
        # disabled. Report the actual terminal reason rather than interpreting
        # get_last_assistant_text(None) as a missing/unavailable Pi runtime.
        # Never trust salvaged JSON/tool arguments from a truncated response.
        usage = message.get("usage")
        tokens = usage.get("output") if isinstance(usage, dict) else None
        output_tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0 else None
        self._emit_event(item)
        self.close()
        raise PiDecisionOutputTruncated(output_tokens=output_tokens)

    def _check_tool_loop(self, item: dict[str, Any]) -> None:
        # Pi emits start before lookup/schema validation, including unknown
        # tools and malformed arguments that never enter our extension execute.
        if item.get("type") == "turn_start" and (
            self._stage_uncertain or self._handoff_requested or self._investigation_count >= 8 or self._tool_attempt_count >= 10
        ) and not self._stage_available():
            if not self._decision_only:
                self._decision_only = True
                self._decision_deadline = time.monotonic() + min(self.timeout_seconds, 90)
        if item.get("type") == "tool_execution_start":
            if item.get("toolName") == "sw_model_stage":
                self._stage_tool_attempts += 1
                if self._stage_tool_attempts > self.stage_attempt_limit or self._decision_only:
                    self._raise_decision_loop("本轮受控建模阶段额度已结束，禁止重复执行；请根据实际检查点返回决策。")
                return
            self._tool_attempt_count += 1
            if self._decision_only or self._tool_attempt_count > 10:
                self._raise_decision_loop(
                    "本轮调查已结束并要求提交阶段决策，但模型仍调用工具；已停止空转，交回任务循环纠正。"
                )

    def _stage_available(self) -> bool:
        return (self.operator is not None and self.stage_execution_allowed and not self._stage_uncertain
                and self._stage_tool_attempts < self.stage_attempt_limit and self._stage_count < self.stage_attempt_limit)

    def _useful_response_progress(self, item: dict[str, Any]) -> bool:
        event = item.get("assistantMessageEvent")
        if (self._decision_only or self._handoff_requested or self._investigation_count >= 8 or self._tool_attempt_count >= 10) and (
            isinstance(event, dict) and event.get("type") == "toolcall_delta"
        ):
            return False
        return self._response_progress(item)

    @staticmethod
    def _response_progress(item: dict[str, Any]) -> bool:
        # Do not extend life for housekeeping, repeated empty events, dialog
        # requests or heartbeats. Content stays private; only activity is used.
        if item.get("type") != "message_update":
            return False
        event = item.get("assistantMessageEvent")
        return isinstance(event, dict) and event.get("type") in {
            "text_delta", "thinking_delta", "toolcall_delta",
        } and isinstance(event.get("delta"), str) and bool(event["delta"].strip())

    def _handle_tool_request(self, item: dict[str, Any]) -> bool:
        """Service trusted extension requests on the CAD operation's own thread."""
        if item.get("type") != "extension_ui_request":
            return False
        if item.get("method") not in {"input", "select", "confirm", "editor"}:
            return False
        request_id = item.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise PiAgentError("Pi 扩展请求缺少合法关联标识。")
        if self._aborted.is_set():
            raise PiAgentError("Pi 会话已停止，拒绝晚到的调查请求。")
        if item.get("method") == "input" and item.get("title") == self._stage_bridge_id:
            return self._handle_stage_request(item, request_id)
        # Count replayed and malformed UI requests separately from real reads.
        # The replay cache is not a budget: previously it permitted unlimited
        # quota-denied requests after the cache filled.
        self._bridge_attempt_count += 1
        if self._bridge_attempt_count > 12:
            self._raise_decision_loop("本轮调查协议请求次数过多，已停止重复调用并交回任务循环纠正。")
        if request_id in self._tool_replies:
            self._write_record(self._tool_replies[request_id])
            return True
        response: dict[str, Any] = {"type": "extension_ui_response", "id": request_id}
        if item.get("method") != "input" or item.get("title") != self._bridge_id or self.investigator is None:
            # An unexpected dialog must never become a permission grant.
            response["cancelled"] = True
        else:
            try:
                if self._investigation_count >= 8 or self._decision_only or self._handoff_requested:
                    raise ValueError("本轮自主调查已结束，请根据已有证据提交下一阶段决策。")
                encoded = item.get("placeholder")
                if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 16_000:
                    raise ValueError("调查请求超过大小限制。")
                request = json.loads(encoded)
                if not isinstance(request, dict) or set(request) != {"tool", "arguments"}:
                    raise ValueError("调查请求结构不合法。")
                if not isinstance(request["tool"], str) or not isinstance(request["arguments"], dict):
                    raise ValueError("调查请求的工具名或参数不合法。")
                self._investigation_count += 1
                result = self.investigator(request["tool"], request["arguments"])
                self._tool_activity += 1
                # This top-level control bit is supplied by the trusted task
                # runner/backend, never inferred from CAD observation data or
                # model tool arguments. It may end investigation before quota.
                if isinstance(result, dict) and result.get("decision_only") is True:
                    self._handoff_requested = True
                if self._aborted.is_set():
                    raise PiAgentError("调查已停止，丢弃晚到结果。")
                value = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                if len(value.encode("utf-8")) > 64_000:
                    raise ValueError("调查结果过大，请缩小范围。")
                response["value"] = value
            except PiAgentError:
                raise
            except Exception as exc:
                error = str(exc).replace(self.api_key, "<redacted>") if self.api_key else str(exc)
                response["value"] = json.dumps({"ok": False, "error": error[:1500]}, ensure_ascii=False)
            # Trusted envelope is generated here, never accepted from model or
            # CAD observations. The provider strips it from observation content.
            response["value"] = json.dumps({
                "result": json.loads(response["value"]),
                "host_control": {
                    "investigation_count": self._investigation_count,
                    "investigation_closed": self._handoff_requested or self._investigation_count >= 8 or self._tool_attempt_count >= 10,
                    "decision_only": (self._handoff_requested or self._investigation_count >= 8 or self._tool_attempt_count >= 10)
                                     and not self._stage_available(),
                },
            }, ensure_ascii=False, allow_nan=False)
        # Bounded cache prevents duplicate execution without retaining whole sessions.
        if len(self._tool_replies) < 16:
            self._tool_replies[request_id] = response
        self._write_record(response)
        return True

    def _handle_stage_request(self, item: dict[str, Any], ui_request_id: str) -> bool:
        """Synchronous, idempotent stage dispatch on the original task thread."""
        self._stage_bridge_attempts += 1
        if self._stage_bridge_attempts > 4:
            self._raise_decision_loop("本轮阶段桥接请求重复过多，已停止；不得重放未知执行结果。")
        response: dict[str, Any] = {"type": "extension_ui_response", "id": ui_request_id}
        request_id: str | None = None
        result: dict[str, Any]
        try:
            encoded = item.get("placeholder")
            if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 36_000:
                raise ValueError("阶段桥接请求超过大小限制")
            request = json.loads(encoded)
            if not isinstance(request, dict) or set(request) != {"request_id", "epoch", "decision", "expires_at"}:
                raise ValueError("阶段桥接请求结构不合法")
            request_id = request["request_id"]
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
                raise ValueError("阶段 request_id 不合法")
            if request["epoch"] != self._stage_epoch or not self._stage_epoch:
                raise ValueError("阶段请求来自已经结束的模型轮次，拒绝晚到操作")
            if not isinstance(request["decision"], dict):
                raise ValueError("阶段决策必须为 JSON 对象")
            decision_json = json.dumps(request["decision"], sort_keys=True, ensure_ascii=False, allow_nan=False)
            if len(decision_json.encode("utf-8")) > 32_768:
                raise ValueError("阶段决策超过 32 KB")
            digest = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
            prior = self._stage_replies.get(request_id)
            if prior is not None:
                if prior["digest"] != digest or prior["epoch"] != self._stage_epoch:
                    raise ValueError("阶段 request_id 已使用，拒绝变更内容或跨轮次重放")
                result = prior["result"]
            else:
                self._stage_count += 1
                expires_at = request["expires_at"]
                if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                        or not 0 < expires_at - time.time() * 1000 <= 635_000):
                    raise ValueError("阶段桥接请求已过期或期限无效，拒绝晚到执行")
                if (self.operator is None or not self.stage_execution_allowed or self._stage_uncertain
                        or self._stage_count > self.stage_attempt_limit or self._decision_only):
                    raise ValueError("当前阶段执行能力不可用或本轮两次尝试预算已耗尽")
                if len(self._stage_replies) >= 128:
                    raise ValueError("阶段去重记录已达安全上限，请在检查点建立新任务")
                unknown = {"ok": False, "status": "unknown", "request_id": request_id,
                           "mutation_attempted": True, "requires_review": True,
                           "error": "阶段已经提交但结果未确定，禁止重放"}
                self._stage_replies[request_id] = {"digest": digest, "epoch": self._stage_epoch, "result": unknown}
                started = time.monotonic()
                try:
                    result = self.operator({"request_id": request_id, "decision": request["decision"]})
                    if not isinstance(result, dict):
                        raise ValueError("阶段执行器未返回结构化结果")
                    value = json.dumps(result, ensure_ascii=False, allow_nan=False)
                    if len(value.encode("utf-8")) > 60_000:
                        raise ValueError("阶段执行结果超过 60 KB")
                    if time.monotonic() - started > 600:
                        raise ValueError("阶段执行超过 600 秒，禁止假定完成或重放")
                    if self._aborted.is_set():
                        raise PiAgentError("阶段执行已停止，丢弃晚到结果；不得重放")
                except Exception as exc:
                    self._stage_uncertain = True
                    error = str(exc).replace(self.api_key, "<redacted>") if self.api_key else str(exc)
                    result = {**unknown, "error": error[:1500]}
                    self._stage_replies[request_id]["result"] = result
                    if self._aborted.is_set():
                        raise PiAgentError("阶段请求已经执行但会话已停止，禁止重放") from exc
                finally:
                    self._stage_elapsed += min(630.0, max(0.0, time.monotonic() - started))
                    self._tool_activity += 1
                if result.get("requires_review") is True or result.get("status") in {"unknown", "stopped"}:
                    self._stage_uncertain = True
                self._stage_replies[request_id]["result"] = result
        except PiAgentError:
            raise
        except Exception as exc:
            error = str(exc).replace(self.api_key, "<redacted>") if self.api_key else str(exc)
            result = {"ok": False, "status": "denied", "request_id": request_id,
                      "mutation_attempted": False, "error": error[:1500]}
        response["value"] = json.dumps({"result": result, "host_control": {
            "stage_count": self._stage_count, "stage_closed": not self._stage_available(),
            "requires_review": self._stage_uncertain,
        }}, ensure_ascii=False, allow_nan=False)
        self._write_record(response)
        return True

    def close(self) -> None:
        with self._lifecycle_lock:
            process = self._process
            self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def abort(self) -> None:
        """Terminal cancellation, including a concurrent first process start."""
        self._aborted.set()
        self.close()

    def _send(self, command_type: str, **payload: Any) -> str:
        request_id = uuid.uuid4().hex
        self._write_record({"id": request_id, "type": command_type, **payload})
        return request_id

    def _write_record(self, body: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            detail = self._diagnostic_summary()
            raise PiAgentError("Pi RPC 已意外退出。" + (f" {detail}" if detail else ""))
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ) + b"\n"
        try:
            with self._write_lock:
                process.stdin.write(encoded)
                process.stdin.flush()
        except OSError as exc:
            raise PiAgentError(f"无法写入 Pi RPC：{exc}") from exc

    def _receive(self, deadline: float) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise PiResponseTimeout(
                    f"Pi 连续 {self.timeout_seconds} 秒没有新的响应进展，已终止本轮会话。"
                )
            try:
                return self._messages.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if not self.running:
                    raise PiAgentError("Pi 会话已停止或意外退出。")

    def _read_stdout(self, stream: Any, messages: queue.Queue[dict[str, Any]] | None = None) -> None:
        buffer = b""
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    self._decode_stdout_record(raw.rstrip(b"\r"), messages)
            if buffer:
                self._decode_stdout_record(buffer.rstrip(b"\r"), messages)
        except (OSError, ValueError) as exc:
            self._remember_diagnostic(f"stdout: {exc}")

    def _decode_stdout_record(self, raw: bytes, messages: queue.Queue[dict[str, Any]] | None = None) -> None:
        if not raw:
            return
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._remember_diagnostic("Pi stdout 包含非 JSON 记录")
            return
        if isinstance(value, dict):
            (messages if messages is not None else self._messages).put(value)

    def _read_stderr(self, stream: Any) -> None:
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self._remember_diagnostic(text)
        except (OSError, ValueError):
            return

    def _remember_diagnostic(self, message: str) -> None:
        safe = message.replace(self.api_key, "<redacted>") if self.api_key else message
        safe = safe[:500]
        if self._diagnostics.full():
            try:
                self._diagnostics.get_nowait()
            except queue.Empty:
                pass
        self._diagnostics.put_nowait(safe)

    def _diagnostic_summary(self) -> str:
        values: list[str] = []
        while True:
            try:
                values.append(self._diagnostics.get_nowait())
            except queue.Empty:
                break
        return " | ".join(values[-3:])[:1200]

    @staticmethod
    def _response_error(item: dict[str, Any], fallback: str) -> str:
        error = item.get("error")
        if isinstance(error, str) and error.strip():
            return f"{fallback}：{error[:1000]}"
        return fallback

    def _emit_event(self, item: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        if item.get("type") == "message_update":
            if self._useful_response_progress(item) and time.monotonic() - self._last_public_progress >= 3:
                self._last_public_progress = time.monotonic()
                self.on_event("response_progress", {})
        else:
            self.on_event(str(item.get("type") or "event"), item)
