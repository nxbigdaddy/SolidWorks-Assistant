"""Length-limited model responses are candidate failures, not offline engines."""
from __future__ import annotations

import json

import pytest

from sw_assistant.agent.pi_client import PiDecisionOutputTruncated
from test_pi_decision_loop import decision_response, native_client


@pytest.mark.parametrize("content", [
    {"reasoning_content": "PRIVATE synthetic reasoning output"},
    {"content": '{"action":"act",'},
    {"content": '{"action":"act","reason":"syntactically complete but truncated"}'},
])
def test_native_pi_length_stop_is_retryable_and_next_prompt_recovers(tmp_path, content):
    def respond(_request, index):
        return ({"role": "assistant", **content}, "length") if index == 1 else decision_response()

    with native_client(tmp_path, respond, lambda *_args: {"ok": True}) as (client, requests):
        with pytest.raises(PiDecisionOutputTruncated, match="输出长度上限") as raised:
            client.prompt("Synthetic task with a previously completed extrusion")
        assert raised.value.retryable_decision is True
        assert "PRIVATE" not in str(raised.value)
        assert "不重复执行" in str(raised.value)
        assert not client.running and not client._aborted.is_set()
        assert json.loads(client.prompt("Keep completed CAD and return the next short decision"))["action"] == "act"
        assert len(requests) == 2
        assert requests[-1].get("tools")


def test_length_stop_reports_only_valid_public_token_count(tmp_path, monkeypatch):
    from test_agent_response_budget import simulated_client

    client, _ = simulated_client(tmp_path, monkeypatch, [])
    with pytest.raises(PiDecisionOutputTruncated) as raised:
        client._check_assistant_completion({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "length", "usage": {"output": 8192},
            "content": [{"type": "thinking", "thinking": "PRIVATE"}],
        }})
    assert raised.value.output_tokens == 8192
    assert "8192" in str(raised.value) and "PRIVATE" not in str(raised.value)
