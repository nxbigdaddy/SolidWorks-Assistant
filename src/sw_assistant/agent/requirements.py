from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any, Callable

import httpx2
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from ..domain import Plan, PlanningError
from ..infrastructure import ModelSettings
from ..tools import ToolRegistry
from .models import PlanningAction, PlanningDecision
from .planning_policy import PlanningAgentPolicy
from .provider_compat import build_agent_model_settings
from .security import MAX_AGENT_CONTEXT_BYTES, scrub_agent_value


ProgressCallback = Callable[[str, str, dict[str, Any]], None]


class RequirementsAgentService:
    """Bounded agent for requirement resolution and initial plan compilation."""

    def __init__(
        self,
        settings: ModelSettings,
        registry: ToolRegistry,
        *,
        max_model_requests: int = 6,
        model: Model | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.max_model_requests = max(2, min(int(max_model_requests), 8))
        self._model_override = model
        self.policy = PlanningAgentPolicy(registry)

    def plan(
        self,
        prompt: str,
        runtime_context: dict[str, Any] | None = None,
        progress: ProgressCallback | None = None,
    ) -> Plan:
        if self.settings.credential_unreadable:
            raise PlanningError("保存的 API Key 无法解密，请重新输入后再启动需求规划 Agent。")
        self.settings.validate()
        if not self.settings.configured and self._model_override is None:
            raise PlanningError("需求规划 Agent 需要先配置模型服务。")

        safe_payload = scrub_agent_value(
            {
                "request": str(prompt).strip(),
                "runtime_context": dict(runtime_context or {}),
            }
        )
        encoded = json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_AGENT_CONTEXT_BYTES:
            raise PlanningError("需求规划 Agent 上下文超过 200 KB 安全上限。")

        self._progress(progress, "agent_preparing", "需求规划 Agent 正在建立只读上下文")
        try:
            decision, inspections = asyncio.run(
                self._run_agent(safe_payload, progress)
            )
        except PlanningError:
            raise
        except Exception as exc:
            raise PlanningError(f"需求规划 Agent 运行失败：{exc}") from exc

        required = {"request", "tools"}
        if not required.issubset(inspections):
            missing = "、".join(sorted(required - inspections))
            raise PlanningError(f"需求规划 Agent 未完成必要的只读检查：{missing}。")

        plan = self.policy.authorize(decision, str(safe_payload["request"]))
        self._progress(
            progress,
            "agent_completed",
            "需求规划 Agent 已生成并通过本地校验的计划",
            action=decision.action.value,
            confidence=decision.confidence,
            assumption_count=len(decision.assumptions),
            step_count=len(plan.steps),
        )
        return plan

    async def _run_agent(
        self,
        safe_payload: dict[str, Any],
        progress: ProgressCallback | None,
    ) -> tuple[PlanningDecision, set[str]]:
        inspections: set[str] = set()
        tools = self.registry.describe()

        def inspect_user_request() -> dict[str, Any]:
            """Read the sanitized user request and current non-sensitive SolidWorks context."""

            inspections.add("request")
            return safe_payload

        def inspect_available_tools() -> dict[str, Any]:
            """Read the exact whitelisted CAD tool schemas; this never executes them."""

            inspections.add("tools")
            return {"tools": tools}

        def inspect_planning_policy() -> dict[str, Any]:
            """Read rules for assumptions, clarification and unsupported geometry."""

            inspections.add("policy")
            return {
                "units": "millimetres unless the request explicitly says otherwise",
                "assumptions": (
                    "Allowed for ordinary non-mating dimensions. Every inferred dimension or "
                    "simplification must be disclosed in assumptions."
                ),
                "clarify_only_when": (
                    "A wrong choice would materially change fit, interfaces, manufacturing intent, "
                    "safety, or the user explicitly requires exact dimensions."
                ),
                "unsupported_geometry": (
                    "Prefer the closest useful simplified model and disclose omissions. "
                    "Do not invent tools."
                ),
                "forbidden": [
                    "shell",
                    "filesystem_write",
                    "dynamic_code",
                    "dll_loading",
                    "direct_com",
                    "saving_documents",
                ],
            }

        async def execute(model: Model) -> PlanningDecision:
            agent = Agent[None, PlanningDecision](
                model,
                deps_type=type(None),
                name="solidworks_requirements_planner",
                output_type=PlanningDecision,
                system_prompt=self._system_prompt(),
                tools=[
                    inspect_user_request,
                    inspect_available_tools,
                    inspect_planning_policy,
                ],
                retries=2,
                model_settings=self._model_settings(),
            )

            @agent.output_validator
            def validate_output(decision: PlanningDecision) -> PlanningDecision:
                missing = {"request", "tools"} - inspections
                if missing:
                    raise ModelRetry(
                        "Before the final decision call the required read-only tools: "
                        + ", ".join(sorted(missing))
                    )
                if decision.action == PlanningAction.CREATE_PLAN:
                    try:
                        self.policy.authorize(decision, str(safe_payload["request"]))
                    except PlanningError as exc:
                        raise ModelRetry(
                            "The local CAD policy rejected the proposed plan. Correct only the "
                            f"rejected fields or sequence and return a new decision: {exc}"
                        ) from exc
                return decision

            self._progress(
                progress,
                "agent_reasoning",
                "需求规划 Agent 正在检查需求、工具能力和安全假设",
            )
            result = await agent.run(
                "先调用必要的只读检查工具，再生成最有用且可执行的结构化建模决策。",
                usage_limits=UsageLimits(
                    request_limit=self.max_model_requests,
                    tool_calls_limit=6,
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
            "你是受约束的 SolidWorks 需求理解与计划 Agent。你不能执行 CAD、Shell、文件写入、"
            "动态代码、DLL、COM 或保存文档；你只有只读检查工具。最终决策前必须调用 "
            "inspect_user_request 和 inspect_available_tools，复杂或不明确时调用 "
            "inspect_planning_policy。优先生成一个有用的安全计划，不要因为普通非装配尺寸缺失就"
            "反复询问用户。可以为普通外形采用合理默认尺寸或简化模型，但每个推断尺寸、近似值、"
            "省略特征都必须在 assumptions 中用中文明确披露。品牌产品没有可靠规格来源时不得宣称"
            "默认值是官方精确尺寸，应说明为近似包络尺寸。只有错误选择会实质影响配合、接口、"
            "制造、安全，或用户明确要求精确规格时，才输出 request_clarification，问题最多三个且"
            "一次问完。当前工具不支持细节时，优先用现有工具建立最接近的简化主体并披露省略项；"
            "不得发明工具。建模顺序通常是 session.ensure、document.new_part、选择基准面、开始草图、"
            "闭合轮廓、实体特征。切除前必须已有实体；端面继续建模使用 sketch.begin_face。"
            "所有 tool 和 arguments 必须来自检查到的 JSON Schema，枚举值不得翻译。"
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
