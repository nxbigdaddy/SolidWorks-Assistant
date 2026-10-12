from __future__ import annotations

import urllib.parse

from pydantic_ai.settings import ModelSettings as AgentModelSettings

from ..infrastructure import ModelSettings


def build_agent_model_settings(settings: ModelSettings) -> AgentModelSettings:
    """Build provider-safe settings for structured planning/recovery tools.

    PydanticAI represents the strict result model as an output tool and may
    force that tool with ``tool_choice``.  DeepSeek V4 enables thinking by
    default, while its Chat Completions endpoint rejects that forced choice in
    thinking mode.  Disable thinking only for the official DeepSeek endpoint;
    the Agent's read-only tools, structured result and local policy remain in
    force.
    """

    model_settings = AgentModelSettings(
        temperature=0.0,
        timeout=settings.timeout_seconds,
        parallel_tool_calls=False,
        max_tokens=8_000,
    )
    host = (urllib.parse.urlsplit(settings.api_base).hostname or "").casefold()
    if host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        model_settings["extra_body"] = {"thinking": {"type": "disabled"}}
    elif host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com"):
        model_settings["extra_body"] = {"enable_thinking": False}
    return model_settings
