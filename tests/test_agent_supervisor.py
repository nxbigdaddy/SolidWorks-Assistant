from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic_ai.models.test import TestModel

from sw_assistant.agent import (
    ProposedToolCall,
    SupervisorAction,
    SupervisorDecision,
    SupervisorPolicy,
    SupervisorService,
)
from sw_assistant.agent.supervisor import _scrub
from sw_assistant.domain import PlanningError, ToolCall
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry


def _context(*, changed: bool = False) -> dict:
    return {
        "original_task": "create corner",
        "completed_steps": [
            ToolCall("session.ensure", {}).to_dict(),
            ToolCall("document.new_part", {}).to_dict(),
            ToolCall("sketch.begin", {"plane": "front"}).to_dict(),
        ],
        "failed_step": ToolCall(
            "sketch.polygon", {"radius_mm": 2, "sides": 6}
        ).to_dict(),
        "recovery_assessment": {
            "category": "geometry",
            "model_changed": changed,
            "state_known": True,
            "safe_retry": False,
            "ai_repair_recommended": not changed,
        },
        "model_state_before": {"body_count": 0, "sketch_entity_count": 0},
        "current_model_state": {
            "body_count": 0,
            "sketch_entity_count": 1 if changed else 0,
        },
        "previous_failed_attempts": [],
    }


def _replacement_args() -> dict:
    return {
        "action": "replace_remaining",
        "reason": "改用圆形闭合轮廓验证受约束替代路径",
        "replacement_steps": [
            {
                "tool": "sketch.circle",
                "arguments": {"radius_mm": 2},
                "description": "创建闭合轮廓",
            },
            {
                "tool": "feature.extrude",
                "arguments": {"depth_mm": 10},
                "description": "创建实体",
            },
        ],
        "expected_postconditions": ["body_count becomes 1"],
        "confidence": 0.9,
    }


def test_supervisor_uses_only_readonly_inspection_and_returns_validated_plan():
    events = []
    service = SupervisorService(
        ModelSettings(),
        build_tool_registry(),
        model=TestModel(call_tools="all", custom_output_args=_replacement_args()),
    )

    outcome = service.supervise(
        _context(),
        lambda stage, message, details: events.append((stage, details)),
    )

    assert outcome.decision.action == SupervisorAction.REPLACE_REMAINING
    assert outcome.plan is not None
    assert [step.tool for step in outcome.plan.steps] == [
        "sketch.circle",
        "feature.extrude",
    ]
    assert events[-1][0] == "agent_completed"


def test_supervisor_can_stop_without_producing_executable_steps():
    service = SupervisorService(
        ModelSettings(),
        build_tool_registry(),
        model=TestModel(
            call_tools="all",
            custom_output_args={
                "action": "request_review",
                "reason": "需要人工确认活动草图",
                "replacement_steps": [],
                "expected_postconditions": [],
                "confidence": 0.4,
            },
        ),
    )

    outcome = service.supervise(_context())

    assert outcome.decision.action == SupervisorAction.REQUEST_REVIEW
    assert outcome.plan is None


def test_policy_rejects_replacement_when_model_changed():
    decision = SupervisorDecision.model_validate(_replacement_args())

    with pytest.raises(PlanningError, match="模型已经变化"):
        SupervisorPolicy(build_tool_registry()).authorize(decision, _context(changed=True))


def test_policy_rejects_exact_repetition_of_failed_call():
    decision = SupervisorDecision(
        action=SupervisorAction.REPLACE_REMAINING,
        reason="repeat",
        replacement_steps=[
            ProposedToolCall(
                tool="sketch.polygon",
                arguments={"radius_mm": 2, "sides": 6},
            )
        ],
        expected_postconditions=["closed sketch"],
        confidence=0.8,
    )

    with pytest.raises(PlanningError, match="重复了已经失败的步骤"):
        SupervisorPolicy(build_tool_registry()).authorize(decision, _context())


def test_policy_rejects_non_finite_tool_arguments():
    decision = SupervisorDecision(
        action=SupervisorAction.REPLACE_REMAINING,
        reason="invalid numeric proposal",
        replacement_steps=[
            ProposedToolCall(tool="sketch.circle", arguments={"radius_mm": float("nan")})
        ],
        expected_postconditions=["closed sketch"],
        confidence=0.8,
    )

    with pytest.raises(PlanningError, match="有限数值"):
        SupervisorPolicy(build_tool_registry()).authorize(decision, _context())


def test_supervisor_scrubs_local_paths_and_credentials():
    value = _scrub(
        {
            "document_path": r"E:\secret\part.SLDPRT",
            "details": r"failed at C:\design\private.SLDPRT",
            "prompt": "make part; api_key=plain-secret; token: abc123",
            "api_key": "plain-secret",
        }
    )

    assert value["document_path"] == "<local-path>"
    assert "C:\\design" not in value["details"]
    assert "plain-secret" not in value["prompt"]
    assert "abc123" not in value["prompt"]
    assert value["api_key"] == "<redacted>"


def test_supervisor_rejects_oversized_context_before_model_call():
    service = SupervisorService(
        ModelSettings(),
        build_tool_registry(),
        model=TestModel(custom_output_args=_replacement_args()),
    )
    context = _context()
    context["oversized"] = "x" * 210_000

    with pytest.raises(PlanningError, match="超过 200 KB"):
        service.supervise(context)


def test_openai_compatible_base_url_strips_chat_completions_suffix():
    service = SupervisorService(
        ModelSettings("https://example.com/v1/chat/completions?api-version=1", "model"),
        build_tool_registry(),
    )

    assert service._base_url() == "https://example.com/v1?api-version=1"

    root_service = SupervisorService(
        ModelSettings("https://api.example.com", "model"),
        build_tool_registry(),
    )
    assert root_service._base_url() == "https://api.example.com"


def test_openai_compatible_agent_performs_readonly_loop_against_local_server():
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
                response = tool_response("inspect_failure", {}, index)
            elif index == 2:
                response = tool_response("inspect_available_tools", {}, index)
            else:
                response = tool_response("final_result", _replacement_args(), index)
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
        f"http://127.0.0.1:{server.server_port}/v1",
        "test-model",
        "test-key",
        10,
    )
    try:
        outcome = SupervisorService(settings, build_tool_registry()).supervise(_context())
    finally:
        server.shutdown()

    assert outcome.plan is not None
    assert len(requests) == 3
    assert {request["path"] for request in requests} == {"/v1/chat/completions"}
    assert {request["authorization"] for request in requests} == {"Bearer test-key"}
    first_tool_names = {
        tool["function"]["name"] for tool in requests[0]["payload"]["tools"]
    }
    assert first_tool_names == {
        "inspect_failure",
        "inspect_available_tools",
        "inspect_recovery_policy",
        "final_result",
    }
