from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic_ai.models.test import TestModel

from sw_assistant.agent import (
    PlanningAction,
    PlanningAgentPolicy,
    PlanningDecision,
    ProposedToolCall,
    RequirementsAgentService,
)
from sw_assistant.domain import ClarificationRequired, PlanningError
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry


def _phone_decision() -> dict:
    return {
        "action": "create_plan",
        "task": "建立手机近似包络主体",
        "reason": "现有工具可以安全建立矩形包络实体",
        "assumptions": [
            "采用 160 × 78 × 9 mm 的近似包络尺寸，不宣称为官方精确规格。",
            "当前工具不支持圆角、按键、接口和摄像头细节，本计划仅建立无圆角主体。",
        ],
        "questions": [],
        "steps": [
            {"tool": "session.ensure", "arguments": {}, "description": "连接 SolidWorks"},
            {"tool": "document.new_part", "arguments": {}, "description": "新建零件"},
            {
                "tool": "selection.plane",
                "arguments": {"plane": "front"},
                "description": "选择前视基准面",
            },
            {
                "tool": "sketch.begin",
                "arguments": {"plane": "front"},
                "description": "开始主体草图",
            },
            {
                "tool": "sketch.rectangle",
                "arguments": {"width_mm": 160, "height_mm": 78},
                "description": "绘制近似包络",
            },
            {
                "tool": "feature.extrude",
                "arguments": {"depth_mm": 9, "operation": "boss", "merge": True},
                "description": "拉伸近似厚度",
            },
        ],
        "confidence": 0.72,
    }


def test_requirements_agent_plans_phone_without_repeating_dimension_questions():
    events = []
    service = RequirementsAgentService(
        ModelSettings(),
        build_tool_registry(),
        model=TestModel(call_tools="all", custom_output_args=_phone_decision()),
    )

    plan = service.plan(
        "画一个 iPhone 17 Pro Max 手机模型",
        {"available": False},
        lambda stage, message, details: events.append((stage, details)),
    )

    assert plan.task == "建立手机近似包络主体"
    assert [step.tool for step in plan.steps][-2:] == [
        "sketch.rectangle",
        "feature.extrude",
    ]
    assert plan.assumptions == _phone_decision()["assumptions"]
    assert events[-1][0] == "agent_completed"
    assert events[-1][1]["assumption_count"] == 2


def test_requirements_agent_respects_two_request_lower_bound():
    service = RequirementsAgentService(
        ModelSettings(),
        build_tool_registry(),
        max_model_requests=2,
        model=TestModel(call_tools="all", custom_output_args=_phone_decision()),
    )

    plan = service.plan("创建手机模型")

    assert service.max_model_requests == 2
    assert plan.assumptions


def test_requirements_agent_can_ask_once_when_fit_dimension_is_material():
    service = RequirementsAgentService(
        ModelSettings(),
        build_tool_registry(),
        model=TestModel(
            call_tools="all",
            custom_output_args={
                "action": "request_clarification",
                "task": "",
                "reason": "轴孔必须与现有轴配合，错误猜测会改变装配意图",
                "assumptions": [],
                "questions": ["配合轴的公称直径和期望间隙是多少毫米？"],
                "steps": [],
                "confidence": 0.9,
            },
        ),
    )

    with pytest.raises(ClarificationRequired, match="公称直径"):
        service.plan("创建一个与现有轴精密配合的孔")


def test_planning_policy_requires_disclosed_inferred_dimensions():
    decision = PlanningDecision.model_validate(_phone_decision())
    decision.assumptions = []

    with pytest.raises(PlanningError, match="没有在 assumptions 中声明"):
        PlanningAgentPolicy(build_tool_registry()).authorize(
            decision,
            "画一个手机模型",
        )


def test_planning_policy_accepts_user_dimensions_without_assumption():
    decision = PlanningDecision(
        action=PlanningAction.CREATE_PLAN,
        task="尺寸明确的矩形实体",
        reason="用户已经提供尺寸",
        assumptions=[],
        questions=[],
        steps=[
            ProposedToolCall(tool="document.new_part", arguments={}),
            ProposedToolCall(tool="sketch.begin", arguments={"plane": "front"}),
            ProposedToolCall(
                tool="sketch.rectangle",
                arguments={"width_mm": 40, "height_mm": 20},
            ),
            ProposedToolCall(tool="feature.extrude", arguments={"depth_mm": 10}),
        ],
        confidence=0.95,
    )

    plan = PlanningAgentPolicy(build_tool_registry()).authorize(
        decision,
        "创建 40 × 20 × 10 mm 的矩形实体",
    )

    assert plan.assumptions == []
    assert plan.steps[-1].arguments["depth_mm"] == 10.0


def test_plan_assumptions_round_trip_through_mapping():
    service = RequirementsAgentService(
        ModelSettings(),
        build_tool_registry(),
        model=TestModel(call_tools="all", custom_output_args=_phone_decision()),
    )
    plan = service.plan("创建手机外形")

    restored = type(plan).from_mapping(plan.to_dict())

    assert restored.assumptions == plan.assumptions


def test_requirements_agent_uses_configured_root_endpoint_without_forcing_v1():
    requests = []

    def tool_response(name, arguments, index):
        return {
            "id": f"chatcmpl-{index}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": json.loads(self.rfile.read(size)),
                }
            )
            index = len(requests)
            if index == 1:
                response = tool_response("inspect_user_request", {}, index)
            elif index == 2:
                response = tool_response("inspect_available_tools", {}, index)
            else:
                response = tool_response("final_result", _phone_decision(), index)
            body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = ModelSettings(
        f"http://127.0.0.1:{server.server_port}",
        "test-model",
        "test-key",
        10,
    )
    try:
        plan = RequirementsAgentService(settings, build_tool_registry()).plan(
            "创建手机模型"
        )
    finally:
        server.shutdown()

    assert plan.assumptions
    assert len(requests) == 3
    assert {request["path"] for request in requests} == {"/chat/completions"}
    assert {request["authorization"] for request in requests} == {"Bearer test-key"}
    tool_names = {
        tool["function"]["name"] for tool in requests[0]["payload"]["tools"]
    }
    assert tool_names == {
        "inspect_user_request",
        "inspect_available_tools",
        "inspect_planning_policy",
        "final_result",
    }
