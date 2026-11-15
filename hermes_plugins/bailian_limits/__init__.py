"""Local Hermes middleware: correct the output cap without replacing its agent loop."""
from urllib.parse import urlsplit


def cap_output(request, base_url="", api_mode="", **kwargs):
    if urlsplit(base_url).hostname != "dashscope.aliyuncs.com" or api_mode != "anthropic_messages":
        return None
    if not isinstance(request, dict):
        return None
    capped = dict(request)
    current = capped.get("max_tokens")
    capped["max_tokens"] = min(current, 8192) if type(current) is int and current > 0 else 8192
    return {"request": capped, "source": "bailian-limits", "reason": "Bound output to the vehicle-preview budget and the provider limit"}


def register(ctx):
    ctx.register_middleware("llm_request", cap_output)
