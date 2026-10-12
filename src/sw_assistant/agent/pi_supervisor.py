from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..domain import PlanningError
from ..infrastructure import ModelSettings
from ..tools import ToolRegistry
from .models import SupervisorAction, SupervisorDecision
from .pi_client import PiAgentError, PiRpcClient, PiRuntime, discover_pi_runtime
from .policy import SupervisorOutcome, SupervisorPolicy
from .security import MAX_AGENT_CONTEXT_BYTES, scrub_agent_value

ProgressCallback = Callable[[str, str, dict[str, Any]], None]


class PiPromptClient(Protocol):
    def prompt(self, message: str) -> str: ...

    def close(self) -> None: ...


class PiSupervisorService:
    """Persistent Pi reasoning session wrapped in the deterministic local policy."""

    def __init__(
        self,
        settings: ModelSettings,
        registry: ToolRegistry,
        state_root: str | Path,
        *,
        session_name: str,
        session_id: str = "",
        max_rounds: int = 6,
        runtime: PiRuntime | None = None,
        client: PiPromptClient | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.state_root = Path(state_root)
        self.session_name = session_name
        self.session_id = session_id
        self.max_rounds = max(2, min(int(max_rounds), 12))
        self.rounds_used = 0
        self.policy = SupervisorPolicy(registry)
        self.runtime = runtime
        self._client = client
        self._supervise_lock = threading.Lock()

    @property
    def backend_source(self) -> str:
        return self.runtime.source if self.runtime is not None else "injected"

    @property
    def rounds_remaining(self) -> int:
        return max(0, self.max_rounds - self.rounds_used)

    def supervise(
        self,
        context: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> SupervisorOutcome:
        with self._supervise_lock:
            return self._supervise_locked(context, progress)

    def _supervise_locked(
        self,
        context: dict[str, Any],
        progress: ProgressCallback | None,
    ) -> SupervisorOutcome:
        if self.settings.credential_unreadable:
            raise PlanningError("保存的 API Key 无法解密，请重新输入后再启动 Pi Agent。")
        self.settings.validate()
        if not self.settings.configured:
            raise PlanningError("Pi Agent 需要先配置模型服务。")
        safe_context = scrub_agent_value(context)
        context_json = json.dumps(
            safe_context, ensure_ascii=False, separators=(",", ":")
        )
        if len(context_json.encode("utf-8")) > MAX_AGENT_CONTEXT_BYTES:
            raise PlanningError("Pi Agent 故障上下文超过 200 KB 安全上限。")
        if self.rounds_remaining == 0:
            return self._exhausted_outcome()

        client = self._ensure_client(progress)
        prompt = self._initial_prompt(context_json)
        while self.rounds_remaining:
            self.rounds_used += 1
            self._progress(
                progress,
                "pi_reasoning",
                "Pi Agent 正在结合历次失败修正恢复方案",
                round=self.rounds_used,
                max_rounds=self.max_rounds,
            )
            try:
                response = client.prompt(prompt)
                decision = self._parse_decision(response)
                self._progress(
                    progress,
                    "pi_authorizing",
                    "正在执行本地安全策略和工具参数校验",
                    round=self.rounds_used,
                )
                outcome = self.policy.authorize(decision, safe_context)
            except (PlanningError, ValidationError, ValueError, json.JSONDecodeError) as exc:
                if not self.rounds_remaining:
                    self._progress(
                        progress,
                        "pi_budget_exhausted",
                        "Pi Agent 多轮修正预算已用完，已转人工检查",
                        rounds_used=self.rounds_used,
                    )
                    return self._exhausted_outcome(str(exc))
                prompt = self._correction_prompt(str(exc), safe_context)
                self._progress(
                    progress,
                    "pi_revision_requested",
                    "候选方案未通过本地校验，已要求同一 Pi 会话继续修正",
                    round=self.rounds_used,
                    remaining=self.rounds_remaining,
                )
                continue
            except PiAgentError as exc:
                raise PlanningError(f"Pi Agent 运行失败：{exc}") from exc

            self._progress(
                progress,
                "pi_completed",
                "Pi Agent 恢复决策已通过本地校验",
                action=outcome.decision.action.value,
                confidence=outcome.decision.confidence,
                replacement_count=len(outcome.plan.steps) if outcome.plan else 0,
                rounds_used=self.rounds_used,
            )
            return outcome
        return self._exhausted_outcome()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None

    def _ensure_client(self, progress: ProgressCallback | None) -> PiPromptClient:
        if self._client is not None:
            return self._client
        self.runtime = self.runtime or discover_pi_runtime()
        self._progress(
            progress,
            "pi_starting",
            "正在启动无工具权限的 Pi 持久恢复会话",
            runtime_source=self.runtime.source,
        )
        self._client = PiRpcClient(
            self.runtime,
            api_base=self._base_url(),
            model=self.settings.model,
            api_key=self.settings.api_key,
            state_root=self.state_root,
            session_name=self.session_name,
            session_id=self.session_id,
            timeout_seconds=self.settings.timeout_seconds,
        )
        return self._client

    def _initial_prompt(self, context_json: str) -> str:
        tools = json.dumps(
            self.registry.describe(), ensure_ascii=False, separators=(",", ":")
        )
        schema = json.dumps(
            SupervisorDecision.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            "你是 SolidWorks 建模失败恢复 Agent。只做推理，不能执行 CAD、COM、Shell、"
            "文件操作、动态代码或 DLL。Python 主程序会独立校验并决定是否执行。\n"
            "请分析本轮失败和同一任务的历史尝试，输出且只输出一个符合 JSON Schema 的 JSON 对象。"
            "若状态未知、模型已变化、没有安全替代方案，action 必须为 request_review 或 stop。"
            "replace_remaining 不得重复完全相同的失败调用，不得重做已完成前缀，不得保存或替换文档。\n"
            f"当前净化后的故障上下文：{context_json}\n"
            f"唯一允许提出的工具及参数 Schema：{tools}\n"
            f"输出 JSON Schema：{schema}"
        )

    @staticmethod
    def _correction_prompt(error: str, context: dict[str, Any]) -> str:
        assessment = context.get("recovery_assessment", {})
        return (
            "上一候选方案被 Python 本地安全策略拒绝，未执行任何 SolidWorks 操作。"
            "请依据拒绝原因修正，并且只输出完整 JSON 对象；不要解释、不要 Markdown。\n"
            f"拒绝原因：{error[:3000]}\n"
            "当前恢复授权摘要："
            + json.dumps(assessment, ensure_ascii=False, separators=(",", ":"))[:10000]
        )

    @staticmethod
    def _parse_decision(text: str) -> SupervisorDecision:
        stripped = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.I)
        if fenced:
            stripped = fenced.group(1).strip()
        candidates = [stripped]
        candidates.extend(stripped[index:] for index, value in enumerate(stripped) if value == "{")
        decoder = json.JSONDecoder()
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                value, _end = decoder.raw_decode(candidate)
                if isinstance(value, dict):
                    return SupervisorDecision.model_validate(value)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
        raise ValueError(f"Pi 输出不是有效的 SupervisorDecision JSON：{last_error}")

    def _base_url(self) -> str:
        parsed = urllib.parse.urlsplit(self.settings.api_base)
        path = parsed.path.rstrip("/")
        suffix = "/chat/completions"
        if path.endswith(suffix):
            path = path[: -len(suffix)]
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, "")
        )

    def _exhausted_outcome(self, error: str = "") -> SupervisorOutcome:
        reason = "Pi Agent 多轮修正预算已用完，需要人工检查当前模型状态和参数。"
        if error:
            reason += " 最后一次拒绝原因：" + error[:800]
        decision = SupervisorDecision(
            action=SupervisorAction.REQUEST_REVIEW,
            reason=reason,
            confidence=0.0,
        )
        return SupervisorOutcome(decision, None)

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        stage: str,
        message: str,
        **details: Any,
    ) -> None:
        if callback:
            callback(stage, message, details)
