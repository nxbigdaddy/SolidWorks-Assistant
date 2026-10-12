from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..agent.requirements import RequirementsAgentService
from ..domain import ClarificationRequired, Plan, PlanningError, ToolCall
from ..infrastructure import ModelSettings
from ..tools import ToolRegistry


MAX_PROMPT_CHARACTERS = 100_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_PLAN_VALIDATION_REPAIRS = 1
ProgressCallback = Callable[[str, str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class SocketHeadCapScrewSpec:
    nominal_diameter_mm: float
    head_diameter_mm: float
    head_height_mm: float
    socket_width_mm: float
    socket_depth_mm: float


class SocketHeadCapScrewPlanner:
    """Compile common ISO 4762 requests without asking a model to invent geometry."""

    _SIZE = re.compile(
        r"(?i)(?:\bM|公称直径\s*)\s*(\d+(?:\.\d+)?)\s*"
        r"(?:[x×*X＊])\s*(\d+(?:\.\d+)?)"
    )
    _INTENT_WORDS = (
        "内六角螺栓",
        "内六角圆柱头",
        "内六角螺钉",
        "socket head cap screw",
        "socket-head cap screw",
        "shcs",
    )
    _SPECS = {
        3.0: SocketHeadCapScrewSpec(3.0, 5.5, 3.0, 2.5, 1.3),
        4.0: SocketHeadCapScrewSpec(4.0, 7.0, 4.0, 3.0, 2.0),
        5.0: SocketHeadCapScrewSpec(5.0, 8.5, 5.0, 4.0, 2.5),
        6.0: SocketHeadCapScrewSpec(6.0, 10.0, 6.0, 5.0, 3.0),
        8.0: SocketHeadCapScrewSpec(8.0, 13.0, 8.0, 6.0, 4.0),
        10.0: SocketHeadCapScrewSpec(10.0, 16.0, 10.0, 8.0, 5.0),
        12.0: SocketHeadCapScrewSpec(12.0, 18.0, 12.0, 10.0, 6.0),
    }

    @classmethod
    def plan(cls, prompt: str) -> Plan | None:
        lowered = prompt.casefold()
        if not any(word in lowered for word in cls._INTENT_WORDS):
            return None
        match = cls._SIZE.search(prompt)
        if match is None:
            raise ClarificationRequired(
                ["请提供内六角螺栓规格和公称长度，例如 M4×30。"]
            )
        diameter = float(match.group(1))
        length = float(match.group(2))
        spec = cls._SPECS.get(diameter)
        if spec is None:
            supported = "、".join(f"M{value:g}" for value in cls._SPECS)
            raise PlanningError(f"当前 ISO 4762 参数库支持 {supported}。")
        if not math.isfinite(length) or not 1.0 <= length <= 1000.0:
            raise PlanningError("内六角螺栓长度必须位于 1 到 1000 mm 之间。")

        socket_radius = spec.socket_width_mm / math.sqrt(3.0)
        task = f"ISO 4762 M{diameter:g}×{length:g} 内六角圆柱头螺钉"
        return Plan(
            task,
            [
                ToolCall("session.ensure", {}, "连接 SolidWorks"),
                ToolCall("document.new_part", {}, "新建零件"),
                ToolCall("selection.plane", {"plane": "front"}, "选择前视基准面"),
                ToolCall("sketch.begin", {"plane": "front"}, "创建螺杆草图"),
                ToolCall(
                    "sketch.circle",
                    {"radius_mm": spec.nominal_diameter_mm / 2.0},
                    f"绘制 M{diameter:g} 公称螺杆截面",
                ),
                ToolCall(
                    "feature.extrude",
                    {"depth_mm": length, "operation": "boss", "merge": True},
                    f"按头部以下公称长度拉伸 {length:g} mm",
                ),
                ToolCall("sketch.begin_face", {}, "在螺杆端面创建头部草图"),
                ToolCall(
                    "sketch.circle",
                    {"radius_mm": spec.head_diameter_mm / 2.0},
                    f"绘制直径 {spec.head_diameter_mm:g} mm 的圆柱头",
                ),
                ToolCall(
                    "feature.extrude",
                    {
                        "depth_mm": spec.head_height_mm,
                        "operation": "boss",
                        "merge": True,
                    },
                    f"拉伸头部高度 {spec.head_height_mm:g} mm",
                ),
                ToolCall("sketch.begin_face", {}, "在螺栓头顶面创建内六角草图"),
                ToolCall(
                    "sketch.polygon",
                    {
                        "radius_mm": socket_radius,
                        "sides": 6,
                        "rotation_deg": 30.0,
                    },
                    f"绘制对边 {spec.socket_width_mm:g} mm 的正六边形",
                ),
                ToolCall(
                    "feature.extrude",
                    {
                        "depth_mm": spec.socket_depth_mm,
                        "operation": "cut",
                        "merge": True,
                    },
                    f"切出深 {spec.socket_depth_mm:g} mm 的内六角孔",
                ),
            ],
        )


class RequirementPreflight:
    _RADIUS_OR_DIAMETER = re.compile(
        r"(?:半径|直径|[RØ⌀])[^\d]{0,10}\d+(?:\.\d+)?", re.I
    )
    _RECTANGLE_SIZE = re.compile(
        r"\d+(?:\.\d+)?\s*(?:毫米|mm)?\s*[x×*]\s*"
        r"\d+(?:\.\d+)?",
        re.I,
    )
    _DEPTH = re.compile(
        r"(?:拉伸|凸台|切除|高度|extrude)[^\d]{0,30}\d+(?:\.\d+)?", re.I
    )
    _THREE_DIMENSIONS = re.compile(
        r"\d+(?:\.\d+)?\s*(?:毫米|mm)?\s*[x×*]\s*"
        r"\d+(?:\.\d+)?\s*(?:毫米|mm)?\s*[x×*]\s*"
        r"\d+(?:\.\d+)?\s*(?:毫米|mm)?",
        re.I,
    )
    _COMPLEX_PRODUCTS = (
        "手机",
        "iphone",
        "smartphone",
        "phone case",
        "手机壳",
    )
    _SIMPLIFICATION_WORDS = (
        "简化",
        "外形",
        "长方体",
        "无圆角",
        "仅主体",
        "basic shape",
        "simplified",
    )

    @classmethod
    def questions(cls, prompt: str) -> list[str]:
        text = str(prompt).strip()
        lowered = text.casefold()
        questions: list[str] = []
        hole_intent = any(word in lowered for word in ("孔", "hole"))
        circle_intent = bool(re.search(r"圆(?!角)", text)) or any(
            word in lowered for word in ("circle", "cylinder")
        )
        if (
            not hole_intent
            and circle_intent
            and not cls._RADIUS_OR_DIAMETER.search(text)
        ):
            questions.append("请提供明确的圆半径或直径（毫米）。")
        if any(word in lowered for word in ("矩形", "长方形", "rectangle")) and not (
            cls._RECTANGLE_SIZE.search(text)
        ):
            questions.append("请提供明确的矩形长和宽，例如 40 x 20 mm。")
        if any(
            word in lowered for word in ("拉伸", "凸台", "切除", "extrude", "圆柱")
        ) and not cls._DEPTH.search(text):
            questions.append("请提供明确的拉伸或切除深度（毫米）。")
        if hole_intent and not cls._RADIUS_OR_DIAMETER.search(text):
            questions.append("请提供孔的明确直径（毫米）。")
        complex_product = any(word in lowered for word in cls._COMPLEX_PRODUCTS)
        if complex_product and not cls._THREE_DIMENSIONS.search(text):
            questions.append(
                "请提供产品整体长×宽×厚（毫米），例如 161 × 78 × 8 mm。"
            )
        if complex_product and not any(
            word in lowered for word in cls._SIMPLIFICATION_WORDS
        ):
            questions.append(
                "请确认是否先创建无圆角的简化主体；当前基础工具尚不能可靠生成"
                "手机圆角、摄像头、按键和接口。"
            )
        return questions[:3]

    @classmethod
    def validate(cls, prompt: str) -> None:
        questions = cls.questions(prompt)
        if questions:
            raise ClarificationRequired(questions)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OfflinePlanner:
    _RADIUS = re.compile(
        r"(?:半径|\bR)[^\d]{0,10}(\d+(?:\.\d+)?)\s*(?:毫米|mm)?", re.I
    )
    _DIAMETER = re.compile(
        r"(?:直径|[Ø⌀])[^\d]{0,10}(\d+(?:\.\d+)?)\s*(?:毫米|mm)?", re.I
    )
    _DEPTH = re.compile(
        r"(?:拉伸|凸台|切除|高度)[^\d]{0,20}(\d+(?:\.\d+)?)\s*(?:毫米|mm)?",
        re.I,
    )
    _RECTANGLE = re.compile(
        r"(?:矩形|长方形)[^\d]{0,20}(\d+(?:\.\d+)?)\s*(?:毫米|mm)?"
        r"\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(?:毫米|mm)?",
        re.I,
    )

    def plan(self, prompt: str) -> Plan:
        text = prompt.strip()
        if not text:
            raise PlanningError("请输入建模需求。")
        plane = "front"
        lowered = text.casefold()
        if "上视" in text or "top plane" in lowered:
            plane = "top"
        elif "右视" in text or "right plane" in lowered:
            plane = "right"

        geometry: ToolCall | None = None
        rectangle = self._RECTANGLE.search(text)
        if rectangle:
            geometry = ToolCall(
                "sketch.rectangle",
                {"width_mm": float(rectangle.group(1)), "height_mm": float(rectangle.group(2))},
                "绘制中心矩形",
            )
        else:
            match = self._RADIUS.search(text)
            radius = float(match.group(1)) if match else None
            if radius is None:
                match = self._DIAMETER.search(text)
                radius = float(match.group(1)) / 2 if match else None
            if radius is not None:
                geometry = ToolCall(
                    "sketch.circle",
                    {"radius_mm": radius},
                    f"绘制半径 {radius:g} mm 的圆",
                )
        if geometry is None:
            raise PlanningError(
                "本地规划器只能识别明确尺寸的圆或矩形；请填写半径/直径，"
                "或配置模型服务处理复杂需求。"
            )

        steps = [
            ToolCall("session.ensure", {}, "连接 SolidWorks"),
            ToolCall("document.new_part", {}, "新建零件"),
            ToolCall("selection.plane", {"plane": plane}, "选择基准面"),
            ToolCall("sketch.begin", {"plane": plane}, "创建草图"),
            geometry,
        ]
        wants_extrude = any(
            word in lowered for word in ("拉伸", "凸台", "切除", "extrude", "圆柱")
        )
        if wants_extrude:
            match = self._DEPTH.search(text)
            if not match:
                raise PlanningError("检测到拉伸意图，但没有找到明确的拉伸深度。")
            steps.append(
                ToolCall(
                    "feature.extrude",
                    {
                        "depth_mm": float(match.group(1)),
                        "operation": "cut" if "切除" in text else "boss",
                        "end_condition": "mid_plane" if "中面" in text else "blind",
                    },
                    "创建拉伸特征",
                )
            )
        return Plan(text, steps)


class ModelClient:
    def __init__(self, settings: ModelSettings, registry: ToolRegistry) -> None:
        self.settings = settings
        self.registry = registry

    def create_plan(self, prompt: str, progress: ProgressCallback | None = None) -> Plan:
        return self._request_plan(prompt, self._system_prompt(), progress)

    def repair_plan(
        self, context: dict[str, Any], progress: ProgressCallback | None = None
    ) -> Plan:
        prompt = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        return self._request_plan(prompt, self._repair_system_prompt(), progress)

    def _request_plan(
        self,
        prompt: str,
        system_prompt: str,
        progress: ProgressCallback | None = None,
    ) -> Plan:
        if self.settings.credential_unreadable:
            raise PlanningError("保存的 API Key 无法解密，请在模型服务设置中重新输入并保存。")
        self.settings.validate()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        self._progress(progress, "requesting", "请求已发送，正在等待结构化计划")
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if self._dashscope():
            payload["enable_thinking"] = False
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(), data=raw, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise PlanningError("模型响应超过 2 MB 安全上限。")
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            provider = self._provider_message(detail)
            if exc.code in {401, 403}:
                message = f"模型服务认证失败（HTTP {exc.code}），请检查 API Key。"
            elif exc.code == 429:
                message = "模型服务限流或额度不足（HTTP 429），请稍后重试。"
            else:
                message = f"模型服务返回 HTTP {exc.code}。"
            raise PlanningError(message + (f" 服务端信息：{provider}" if provider else "")) from exc
        except PlanningError:
            raise
        except Exception as exc:
            raise PlanningError(f"无法调用模型服务：{exc}") from exc

        self._progress(
            progress,
            "response_received",
            "已收到模型响应，正在解析 JSON",
            response_bytes=len(body),
        )
        try:
            envelope = json.loads(body.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise PlanningError("模型服务没有返回有效的 Chat Completions JSON。") from exc
        return Plan.from_mapping(self._parse_content(content))

    def _system_prompt(self) -> str:
        tools = json.dumps(self.registry.describe(), ensure_ascii=False, separators=(",", ":"))
        return (
            "你是 SolidWorks 计划编译器。只输出一个 JSON 对象，不输出 Markdown 或解释。"
            "格式为 {\"task\":\"...\",\"steps\":[{\"tool\":\"工具名\","
            "\"arguments\":{},\"description\":\"...\"}]}。"
            "尺寸使用毫米。建模顺序必须是新建零件、创建草图、绘制几何、创建特征；"
            "切除前必须已有实体。用于实体特征的每组直线必须首尾连接形成闭合轮廓，"
            "中心线是构造线，不能代替轮廓闭合边。正多边形必须使用 sketch.polygon。"
            "标准基准面的草图局部坐标约定为：front 的 (x,y) 对应世界 (X,Y)，"
            "top 的 (x,y) 对应世界 (X,-Z)，right 的 (x,y) 对应世界 (Y,Z)。"
            "例如长实体终点位于世界 Z=90 mm 时，right 草图中心应使用 y_mm=90。"
            "在已有实体的端面继续建模必须使用 sketch.begin_face，不能用原点基准面冒充端面。"
            "工具 arguments 使用 JSON Schema 描述；type、enum、minimum、maximum、required 和 "
            "additionalProperties 都是强约束，枚举值必须原样输出，不能翻译或改写。"
            "不要保存文件，不要调用未列出的工具。可用工具：" + tools
        )

    def _repair_system_prompt(self) -> str:
        tools = json.dumps(self.registry.describe(), ensure_ascii=False, separators=(",", ":"))
        return (
            "你是 SolidWorks 失败恢复计划编译器。输入是已脱敏的失败上下文。"
            "只输出从失败位置开始的替代后缀，不得重复已经成功的步骤。"
            "只输出 JSON 对象，格式为 {\"task\":\"修复说明\",\"steps\":["
            "{\"tool\":\"工具名\",\"arguments\":{},\"description\":\"...\"}]}。"
            "修复后的实体轮廓必须闭合；不得把中心线当作闭合边。"
            "必须根据 recovery_assessment、model_state_before、current_model_state 和"
            "previous_failed_attempts 判断责任阶段；不得返回历史中已经以相同参数失败的步骤。"
            "模型状态已变化或未知时不得假设失败步骤已回滚。"
            "草图局部坐标约定：front(x,y)->world(X,Y)，top(x,y)->world(X,-Z)，"
            "right(x,y)->world(Y,Z)。"
            "后续草图位于已有特征端面时使用 sketch.begin_face。"
            "工具 arguments 使用 JSON Schema 描述，所有枚举值必须原样输出。"
            "不得保存文件，不得调用未列出的工具。可用工具：" + tools
        )

    def _url(self) -> str:
        parsed = urllib.parse.urlsplit(self.settings.api_base)
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def _dashscope(self) -> bool:
        host = (urllib.parse.urlsplit(self.settings.api_base).hostname or "").casefold()
        return host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com")

    @staticmethod
    def _provider_message(detail: str) -> str:
        try:
            payload = json.loads(detail)
            error = payload.get("error", {})
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or "")[:300]
            return str(payload.get("message") or "")[:300]
        except (json.JSONDecodeError, AttributeError):
            return detail.strip()[:300]

    @staticmethod
    def _parse_content(content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        text = str(content).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanningError(f"模型没有返回有效 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise PlanningError("模型返回的 JSON 顶层必须是对象。")
        return value

    @staticmethod
    def _progress(
        callback: ProgressCallback | None, stage: str, message: str, **details: Any
    ) -> None:
        if callback:
            callback(stage, message, details)


class PlannerService:
    def __init__(
        self,
        settings: ModelSettings,
        registry: ToolRegistry,
        *,
        use_agent: bool = False,
        max_model_requests: int = 6,
        runtime_context: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.use_agent = bool(use_agent)
        self.max_model_requests = max(2, min(int(max_model_requests), 8))
        self.runtime_context = dict(runtime_context or {})

    def plan(self, prompt: str, progress: ProgressCallback | None = None) -> Plan:
        text = str(prompt).strip()
        if not text:
            raise PlanningError("请输入建模需求。")
        if len(text) > MAX_PROMPT_CHARACTERS:
            raise PlanningError("建模需求超过 100000 个字符安全上限。")
        if progress:
            progress("preparing", "正在整理需求和可用工具", {})
        fastener_plan = SocketHeadCapScrewPlanner.plan(text)
        model_client: ModelClient | None = None
        if fastener_plan is not None:
            if progress:
                progress("deterministic", "正在套用 ISO 4762 标准件参数", {})
            plan = fastener_plan
        elif self.settings.configured and self.use_agent:
            plan = RequirementsAgentService(
                self.settings,
                self.registry,
                max_model_requests=self.max_model_requests,
            ).plan(text, self.runtime_context, progress)
        elif self.settings.configured:
            RequirementPreflight.validate(text)
            model_client = ModelClient(self.settings, self.registry)
            plan = model_client.create_plan(text, progress)
        else:
            RequirementPreflight.validate(text)
            if progress:
                progress("offline", "正在使用本地规则规划器", {})
            plan = OfflinePlanner().plan(text)
        if progress:
            progress("validating", f"正在校验 {len(plan.steps)} 个步骤", {})
        try:
            validated = self.registry.validate_plan(plan.steps)
        except PlanningError as exc:
            self._attach_plan_diagnostics(exc, plan)
            if model_client is None:
                raise
            validated = self._repair_validation_failure(
                text,
                plan,
                exc,
                model_client,
                progress,
            )
        result = Plan(
            plan.task,
            validated,
            plan.id,
            plan.created_at,
            list(plan.assumptions),
        )
        if progress:
            progress("completed", f"计划已通过校验，共 {len(validated)} 步", {})
        return result

    def _repair_validation_failure(
        self,
        prompt: str,
        plan: Plan,
        initial_error: PlanningError,
        client: ModelClient,
        progress: ProgressCallback | None,
    ) -> list[ToolCall]:
        """Ask for one corrected suffix while preserving the valid prefix."""

        details = dict(initial_error.details)
        step_index = self._failed_step_index(initial_error, plan)
        prefix = plan.steps[: max(0, step_index - 1)]
        try:
            prefix = self.registry.validate_plan(prefix)
        except PlanningError:
            prefix = []
            step_index = 1
        failed_offset = step_index - 1
        context = {
            "repair_kind": "planning_validation",
            "original_request": prompt,
            "original_task": plan.task,
            "validation_error": str(initial_error),
            "error_details": {
                key: value
                for key, value in details.items()
                if key != "failed_plan"
            },
            "completed_steps": [step.to_dict() for step in prefix],
            "failed_step": plan.steps[failed_offset].to_dict(),
            "remaining_steps": [
                step.to_dict() for step in plan.steps[failed_offset + 1 :]
            ],
            "instruction": (
                "保留 completed_steps，只返回从 failed_step 开始的完整替代后缀；"
                "修正 validation_error 指出的参数并遵守工具 JSON Schema。"
            ),
        }
        if progress:
            progress(
                "repairing_validation",
                f"第 {step_index} 步未通过校验，正在定向修复计划后缀",
                {
                    "repair_attempt": MAX_PLAN_VALIDATION_REPAIRS,
                    "failed_step_index": step_index,
                    "failed_tool": plan.steps[failed_offset].tool,
                },
            )
        try:
            repaired = client.repair_plan(context, progress)
            if progress:
                progress(
                    "validating_repair",
                    f"正在校验 {len(repaired.steps)} 个修复步骤",
                    {"repair_attempt": MAX_PLAN_VALIDATION_REPAIRS},
                )
            combined = prefix + repaired.steps
            return self.registry.validate_plan(combined)
        except PlanningError as repair_error:
            repair_details = dict(repair_error.details)
            repair_details.update(
                {
                    "initial_error": str(initial_error),
                    "repair_attempts": MAX_PLAN_VALIDATION_REPAIRS,
                    "failed_step_index": step_index,
                    "failed_plan": plan.to_dict(),
                }
            )
            raise PlanningError(
                "模型计划未通过本地校验，自动定向修复后仍然无效："
                f"{repair_error}",
                details=repair_details,
            ) from repair_error

    @staticmethod
    def _attach_plan_diagnostics(error: PlanningError, plan: Plan) -> None:
        error.details.setdefault("step_count", len(plan.steps))
        error.details.setdefault("failed_plan", plan.to_dict())
        match = re.search(r"第\s*(\d+)\s*步", str(error))
        if match:
            step_index = int(match.group(1))
            if 1 <= step_index <= len(plan.steps):
                step = plan.steps[step_index - 1]
                error.details.setdefault("step_index", step_index)
                error.details.setdefault("step_id", step.id)
                error.details.setdefault("tool", step.tool)

    @staticmethod
    def _failed_step_index(error: PlanningError, plan: Plan) -> int:
        value = error.details.get("step_index")
        if isinstance(value, int) and 1 <= value <= len(plan.steps):
            return value
        match = re.search(r"第\s*(\d+)\s*步", str(error))
        if match:
            parsed = int(match.group(1))
            if 1 <= parsed <= len(plan.steps):
                return parsed
        return 1

    def repair(
        self, context: dict[str, Any], progress: ProgressCallback | None = None
    ) -> Plan:
        if not self.settings.configured:
            raise PlanningError("模型修复需要先配置模型服务。")
        if progress:
            progress("preparing", "正在整理失败步骤和当前模型状态", {})
        plan = ModelClient(self.settings, self.registry).repair_plan(context, progress)
        if progress:
            progress("validating", f"正在校验 {len(plan.steps)} 个替代步骤", {})
        prefix_values = context.get("completed_steps", [])
        if not isinstance(prefix_values, list):
            raise PlanningError("修复上下文中的已完成步骤格式无效。")
        prefix = [ToolCall.from_mapping(value) for value in prefix_values]
        combined = self.registry.validate_plan(prefix + plan.steps)
        validated = combined[len(prefix) :]
        result = Plan(
            plan.task,
            validated,
            plan.id,
            plan.created_at,
            list(plan.assumptions),
        )
        if progress:
            progress("completed", f"替代后缀已通过校验，共 {len(validated)} 步", {})
        return result
