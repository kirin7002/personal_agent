"""Prove that a new JSON tool works with Hermes without changing code or prompts."""
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOOL_ID = "control.climate.set_purification"


async def main():
    path = ROOT / "capabilities/tools/control/climate/_extension_probe.json"
    if path.exists():
        raise RuntimeError("验证文件已存在，不覆盖")
    definition = {
        "id": TOOL_ID, "version": "1.0.0", "kind": "control", "category_path": ["control", "climate"],
        "title": "设置空调净化强度", "description": "生成空调空气净化强度的调用结构；不执行真实控制。", "aliases": ["净化", "purification"], "enabled": True,
        "input_schema": {"type": "object", "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 3}}, "required": ["level"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"prepared": {"const": True}}, "required": ["prepared"], "additionalProperties": False},
        "mock": {"mode": "prepare_only"}, "examples": [{"instruction": "空调净化强度设为二档", "arguments": {"level": 2}}],
    }
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=20, trust_env=False) as client:
        (await client.post("/api/session")).raise_for_status()
        before = (await client.get("/api/catalog")).json()
        path.write_text(json.dumps(definition, ensure_ascii=False, indent=2)+"\n")
        try:
            (await client.post("/api/catalog/reload")).raise_for_status()
            task = (await client.post("/api/tasks", json={"goal": "把空调净化强度设为二档", "request_key": uuid.uuid4().hex})).json()
            deadline = time.monotonic()+140
            while time.monotonic() < deadline:
                task = (await client.get("/api/tasks/"+task["id"])).json()
                if task["status"] != "running":
                    break
                await asyncio.sleep(.5)
            assert task["status"] == "generated", task.get("summary")
            assert [{"tool": c["tool"], "arguments": c["arguments"]} for c in task["calls"]] == [{"tool": TOOL_ID, "arguments": {"level": 2}}]
            result = {"passed": True, "method": "add_json_then_reload", "system_prompt_changed": False, "new_tool": TOOL_ID, "generated_arguments": {"level": 2}, "executed": False}
        finally:
            path.unlink()
            (await client.post("/api/catalog/reload")).raise_for_status()
        after = (await client.get("/api/catalog")).json()
        assert after["catalog_revision"] == before["catalog_revision"]
        # The removed probe remains readable in its task's immutable snapshot.
        history = (await client.get("/api/tasks/"+task["id"])).json()
        assert history["output"]["calls"][0]["tool"] == TOOL_ID
        result["original_catalog_restored"] = True
        (ROOT/"test-results/live-config-extension.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
