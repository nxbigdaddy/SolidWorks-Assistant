"""Extent evidence uses the same constrained native Pi bridge, with no CAD."""
from __future__ import annotations

import json
from typing import get_args

from sw_assistant.agent.task_backend import INVESTIGATION_TOOLS, InvestigationTool, TaskAgentBackend
from sw_assistant.domain import ToolCall
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.tools import build_tool_registry
from test_pi_decision_loop import native_client, tool_response


def test_native_pi_extent_investigation_and_task_schema_share_read_only_contract(tmp_path):
    evidence = {"complete": True, "document_identity": "synthetic-doc", "document_title": "Synthetic",
                "document_path": "", "configuration": "Default", "configuration_identity": "synthetic-config",
                "source": "IBody2.GetExtremePoint", "coordinate_system": "model", "bodies_count": 1,
                "extents_mm": {"x": 10.0, "y": 10.0, "z": 10.0}, "issues": []}
    registry = build_tool_registry()
    calls = []
    backend = TaskAgentBackend(ModelSettings("http://127.0.0.1:1/v1", "synthetic", "fixture"),
                               registry, tmp_path / "backend", session_id="extent-integration", backend="pi")
    backend.set_investigator(lambda name, args: calls.append((name, args)) or {"ok": True, "data": evidence})

    def respond(_request, index):
        if index == 1:
            return tool_response(index, tool="model.measure_extents")
        return {"role": "assistant", "content": json.dumps({
            "action": "inspect", "reason": "Need independent final verification of measured axes",
            "steps": [{"tool": "model.measure_extents", "arguments": {}}],
            "goals": [{"id": f"extent_{axis}", "description": f"Overall {axis} extent", "metric": "extent_mm",
                       "target": axis, "binding": "fixed", "expected": 10} for axis in ("x", "y", "z")],
        })}, "stop"

    try:
        with native_client(tmp_path / "native", respond, backend._investigate) as (client, requests):
            decision = backend._parse(client.prompt("Synthetic 10 mm part extent check"))
            assert calls == [("model.measure_extents", {})]
            assert {goal.target for goal in decision.goals} == {"x", "y", "z"}
            assert registry.validate(ToolCall("model.measure_extents", {})).arguments == {}
            assert registry.write_scope("model.measure_extents") == "none"
            parameters = requests[0]["tools"][0]["function"]["parameters"]
            choices = parameters["properties"]["tool"]["anyOf"]
            native_tools = {choice["const"] for choice in choices}
            assert native_tools == set(get_args(InvestigationTool)) == INVESTIGATION_TOOLS
            assert "document.new_part" not in native_tools and "feature.extrude" not in native_tools
            tool_results = [message for message in requests[1]["messages"] if message["role"] == "tool"]
            assert len(tool_results) == 1
            payload = json.loads(tool_results[0]["content"])
            assert payload["ok"] is True and payload["data"]["extents_mm"] == evidence["extents_mm"]
            assert payload["data"]["source"] == "IBody2.GetExtremePoint"
    finally:
        backend.close()
