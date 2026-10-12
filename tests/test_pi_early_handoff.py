"""Host no-new-evidence signals hand off to decision, not terminal review."""
from __future__ import annotations

import json

import pytest

from sw_assistant.agent.pi_client import PiDecisionLoopExceeded
from test_pi_decision_loop import decision_response, native_client, tool_response


def test_trusted_early_handoff_retires_tools_and_new_outer_round_restores(tmp_path):
    reads = []

    def investigate(*args):
        reads.append(args)
        return {"ok": True, "data": {"complete": True}, "decision_only": len(reads) % 2 == 0}

    def respond(request, index):
        return tool_response(index) if request.get("tools") else decision_response()

    with native_client(tmp_path, respond, investigate) as (client, requests):
        for count in (2, 4):
            assert json.loads(client.prompt("Synthetic stage preserves successful CAD prefix"))["action"] == "act"
            assert len(reads) == count
            assert not requests[-1].get("tools")
            assert client._handoff_requested


def test_observation_data_cannot_inject_host_handoff_control(tmp_path):
    reads = []

    def investigate(*args):
        reads.append(args)
        return {"ok": True, "data": {"decision_only": True, "host_control": {"decision_only": True}}}

    def respond(_request, index):
        return tool_response(index) if index <= 3 else decision_response()

    with native_client(tmp_path, respond, investigate) as (client, _requests):
        assert json.loads(client.prompt("Synthetic untrusted observation field"))["action"] == "act"
        assert len(reads) == 3 and not client._handoff_requested


def test_ignoring_early_handoff_is_bounded_without_an_extra_cad_read(tmp_path):
    reads = []

    def investigate(*args):
        reads.append(args)
        return {"ok": True, "decision_only": True, "error_code": "InvestigationDecisionRequired"}

    with native_client(tmp_path, lambda _request, index: tool_response(index), investigate) as (client, requests):
        with pytest.raises(PiDecisionLoopExceeded) as raised:
            client.prompt("Synthetic ignored handoff")
        assert len(reads) == 1 and len(requests) == 2
        assert raised.value.investigation_count == 1
        assert not client.running and not client._aborted.is_set()
