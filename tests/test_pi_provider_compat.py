"""Provider switches are endpoint-scoped and tested without external requests."""
from __future__ import annotations

import json

import pytest

from sw_assistant.agent.pi_client import PiRpcClient, PiRuntime
from test_pi_decision_loop import decision_response, native_client


@pytest.mark.parametrize(("url", "expected"), [
    ("https://api.deepseek.com", {"thinking": {"type": "disabled"}}),
    ("https://api.deepseek.com/v1", {"thinking": {"type": "disabled"}}),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://other.example/v1", {}),
    ("https://deepseek.com.other.example/v1", {}),
])
def test_pi_environment_reuses_endpoint_specific_body_policy(tmp_path, url, expected):
    client = PiRpcClient(PiRuntime(tmp_path, tmp_path, tmp_path, "fixture"), api_base=url,
                         model="deepseek-v4-pro", api_key="fixture", state_root=tmp_path, session_name="policy")
    assert json.loads(client.child_environment["SW_ASSISTANT_PI_REQUEST_BODY"]) == expected


@pytest.mark.parametrize("extra", [{"thinking": {"type": "disabled"}}, {"enable_thinking": False}, {}])
def test_native_pi_sends_only_host_approved_compat_fields_to_local_http(tmp_path, monkeypatch, extra):
    original = PiRpcClient.child_environment

    def local_environment(client):
        environment = original.fget(client)
        # Synthetic hook injection only. The URL remains 127.0.0.1, never the
        # official endpoint used by the pure policy assertions above.
        assert client.api_base.startswith("http://127.0.0.1:")
        environment["SW_ASSISTANT_PI_REQUEST_BODY"] = json.dumps({**extra, "tools": ["unsafe"]})
        return environment

    monkeypatch.setattr(PiRpcClient, "child_environment", property(local_environment))
    with native_client(tmp_path, lambda *_args: decision_response(), lambda *_args: {"ok": True}) as (client, requests):
        client.prompt("Synthetic compatibility request")
        request = requests[0]
        for key in ("thinking", "enable_thinking"):
            if key in extra:
                assert request[key] == extra[key]
            else:
                assert key not in request
        assert {tool["function"]["name"] for tool in request["tools"]} == {"sw_investigate"}
