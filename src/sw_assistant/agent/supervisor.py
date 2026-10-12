from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any, Callable

import httpx2
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from ..domain import PlanningError
from ..infrastructure import ModelSettings
from ..tools import ToolRegistry
from .models import SupervisorDecision
from .policy import SupervisorOutcome, SupervisorPolicy
from .provider_compat import build_agent_model_settings
from .security import MAX_AGENT_CONTEXT_BYTES, scrub_agent_value as _scrub

ProgressCallback = Callable[[str, str, dict[str, Any]], None]

class SupervisorService:
    """PydanticAI-backed, read-only supervisor for a paused execution failure."""

    def __init__(
        self,
        settings: ModelSettings,
        registry: ToolRegistry,
        *,
        max_model_requests: int = 4,
        model: Model | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.max_model_requests = max(2, min(int(max_model_requests), 8))
        self._model_override = model
        self.policy = SupervisorPolicy(registry)

    def supervise(
        self,
        context: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> SupervisorOutcome:
        if self.settings.credential_unreadable:
            raise PlanningError("保存的 API Key 无法解密，请重新输入后再启动监督 Agent。")
        self.settings.validate()
        if not self.settings.configured and self._model_override is None:
            raise PlanningError("监督 Agent 需要先配置模型服务。")

        safe_context = _scrub(context)
        encoded = json.dumps(safe_context, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_AGENT_CONTEXT_BYTES:
            raise PlanningError("监督 Agent 故障上下文超过 200 KB 安全上限。")
        tools = self.registry.describe()
        self._progress(progress, "agent_preparing", "正在建立只读监督上下文")
        try:
            decision, inspections = asyncio.run(
                self._run_agent(safe_context, tools, progress)
            )
        except PlanningError:
            raise
        except Exception as exc:
            raise PlanningError(f"监督 Agent 运行失败：{exc}") from exc

        required = {"failure", "tools"}
        if not required.issubset(inspections):
            missing = "、".join(sorted(required - inspections))
            raise PlanningError(f"监督 Agent 未完成必要的只读检查：{missing}。")
        self._progress(progress, "agent_authorizing", "正在执行本地安全策略和计划校验")
        outcome = self.policy.authorize(decision, safe_context)
        self._progress(
            progress,
            "agent_completed",
            "监督 Agent 决策已完成",
            action=decision.action.value,
            confidence=decision.confidence,
            replacement_count=len(outcome.plan.steps) if outcome.plan else 0,
        )
        return outcome

    async def _run_agent(
        self,
        context: dict[str, Any],
        tools: list[dict[str, Any]],
        progress: ProgressCallback | None,
    ) -> tuple[SupervisorDecision, set[str]]:
        inspections: set[str] = set()

        def inspect_failure() -> dict[str, Any]:
            """Read the sanitized failed step, model snapshots and previous attempts."""

            inspections.add("failure")
            return context

        def inspect_available_tools() -> dict[str, Any]:
            """Read the exact whitelisted SolidWorks tool schemas; this never executes them."""

            inspections.add("tools")
            return {"tools": tools}

        def inspect_recovery_policy() -> dict[str, Any]:
            """Read the non-negotiable local recovery authorization policy."""

            inspections.add("policy")
            return {
                "replace_allowed_only_when": (
                    "state_known=true, model_changed=false, ai_repair_recommended=true"
                ),
                "forbidden_capabilities": [
                    "shell",
                    "filesystem_write",
                    "dynamic_code",
                    "dll_loading",
                    "direct_com",
                ],
                "exact_failed_call_must_not_repeat": True,
                "all_steps_are_locally_validated": True,
            }

        async def execute(model: Model) -> SupervisorDecision:
            agent = Agent[None, SupervisorDecision](
                model,
                deps_type=type(None),
                name="solidworks_recovery_supervisor",
                output_type=SupervisorDecision,
                system_prompt=self._system_prompt(),
                tools=[inspect_failure, inspect_available_tools, inspect_recovery_policy],
                retries=1,
                model_settings=self._model_settings(),
            )
            self._progress(progress, "agent_reasoning", "监督 Agent 正在检查失败和可用工具")
            result = await agent.run(
                "检查当前 SolidWorks 失败。必须先调用只读检查工具，再输出受约束的恢复决策。",
                usage_limits=UsageLimits(
                    request_limit=self.max_model_requests,
                    tool_calls_limit=3,
                    output_tokens_limit=8_000,
                ),
            )
            return result.output

        if self._model_override is not None:
            return await execute(self._model_override), inspections

        async with httpx2.AsyncClient(follow_redirects=False) as http_client:
            provider = OpenAIProvider(
                base_url=self._base_url(),
                api_key=self.settings.api_key or "not-required",
                http_client=http_client,
            )
            model = OpenAIChatModel(self.settings.model, provider=provider)
            return await execute(model), inspections

    def _base_url(self) -> str:
        parsed = urllib.parse.urlsplit(self.settings.api_base)
        path = parsed.path.rstrip("/")
        suffix = "/chat/completions"
        if path.endswith(suffix):
            path = path[: -len(suffix)]
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, "")
        )

    def _model_settings(self):
        return build_agent_model_settings(self.settings)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是一个受约束的 SolidWorks 失败恢复监督 Agent。"
            "你不能执行 CAD、Shell、文件写入、动态代码、DLL 或 COM；你只有只读检查工具。"
            "在最终决策前必须调用 inspect_failure 和 inspect_available_tools，必要时调用"
            " inspect_recovery_policy。若模型状态未知、已经变化或策略不允许 AI 修复，"
            "输出 request_review 或 stop，replacement_steps 必须为空。"
            "只有 recovery_assessment 明确允许时才能输出 replace_remaining。"
            "替代步骤必须使用工具白名单中的精确名称和 JSON 参数，不得重复历史中完全相同的失败调用，"
            "不得重建已经成功的前缀，不得保存或替换用户文档。"
            "expected_postconditions 必须描述可由本地快照或特征读回验证的结果。"
            "confidence 只是诊断信息，不会绕过本地策略。"
        )

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        stage: str,
        message: str,
        **details: Any,
    ) -> None:
        if callback:
            callback(stage, message, details)
