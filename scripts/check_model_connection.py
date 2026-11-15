"""Minimal authenticated tool-call probe; never prints credentials or raw errors."""
import asyncio
import argparse
import json
from pathlib import Path
import re
import sys

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct", action="store_true", help="Ignore proxy environment for this explicit connectivity probe")
    args = parser.parse_args()
    env = dotenv_values(ROOT / ".env")
    base = env.get("BAILIAN_ANTHROPIC_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic")
    key = env.get("DASHSCOPE_API_KEY")
    if not key:
        print(json.dumps({"ok": False, "error": "missing_api_key"}))
        return 1
    if base != "https://dashscope.aliyuncs.com/apps/anthropic":
        print(json.dumps({"ok": False, "error": "unexpected_endpoint"}))
        return 1
    model = env.get("BAILIAN_MODEL", "deepseek-v4.1-flash-1017")
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=False, trust_env=not args.direct) as client:
            response = await client.post(base + "/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json={
                "model": model, "max_tokens": 2048,
                "messages": [{"role": "user", "content": "把所有分区空调设置为23摄氏度。仅生成工具参数，不执行。"}],
                "tools": [{"name": "climate_set_temperature", "description": "仅生成空调温度参数，不执行车辆操作", "input_schema": {"type": "object", "properties": {"zone": {"type": "string", "enum": ["all"]}, "temperature_c": {"type": "number"}}, "required": ["zone", "temperature_c"]}}],
                "tool_choice": {"type": "tool", "name": "climate_set_temperature"},
            })
        result = {"ok": response.is_success, "http_status": response.status_code, "model": model}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success:
            result["stop_reason"] = body.get("stop_reason")
            result["calls"] = [{"tool": b.get("name"), "arguments": b.get("input")} for b in body.get("content", []) if b.get("type") == "tool_use"]
            result["ok"] = any(c["tool"] == "climate_set_temperature" and c["arguments"] == {"zone": "all", "temperature_c": 23} for c in result["calls"]) and body.get("stop_reason") != "max_tokens"
        else:
            error = body.get("error", {})
            code = error.get("type", "request_rejected") if isinstance(error, dict) else "request_rejected"
            result["error"] = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "request_rejected"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except httpx.HTTPError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
