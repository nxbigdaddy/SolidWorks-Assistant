from sw_assistant.agent.provider_compat import build_agent_model_settings
from sw_assistant.infrastructure import ModelSettings


def test_official_deepseek_disables_thinking_for_structured_agent_tools():
    settings = build_agent_model_settings(
        ModelSettings("https://api.deepseek.com", "deepseek-v4-pro", "secret")
    )

    assert settings["extra_body"] == {"thinking": {"type": "disabled"}}
    assert settings["parallel_tool_calls"] is False


def test_dashscope_keeps_its_provider_specific_non_thinking_switch():
    settings = build_agent_model_settings(
        ModelSettings(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-plus",
            "secret",
        )
    )

    assert settings["extra_body"] == {"enable_thinking": False}


def test_generic_openai_compatible_endpoint_is_not_modified():
    settings = build_agent_model_settings(
        ModelSettings("https://model.example.com/v1", "custom-model", "secret")
    )

    assert "extra_body" not in settings
