from hermes_plugins.bailian_limits import cap_output


def test_cap_unknown_model_default_and_preserve_request():
    original = {"model": "qwen-plus", "max_tokens": 65535, "messages": [{"role": "user", "content": "demo"}]}
    result = cap_output(original, base_url="https://dashscope.aliyuncs.com/apps/anthropic", api_mode="anthropic_messages")
    assert result["request"]["max_tokens"] == 8192
    assert original["max_tokens"] == 65535
    assert result["request"]["messages"] == original["messages"]


def test_do_not_override_smaller_budget_or_other_endpoints():
    request = {"max_tokens": 128}
    assert cap_output(request, base_url="https://dashscope.aliyuncs.com/apps/anthropic", api_mode="anthropic_messages")["request"]["max_tokens"] == 128
    assert cap_output(request, base_url="https://example.com/anthropic", api_mode="anthropic_messages") is None
    assert cap_output(request, base_url="https://dashscope.aliyuncs.com/apps/anthropic", api_mode="chat_completions") is None
