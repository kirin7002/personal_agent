"""Opt-in integration checks against the configured local Hermes + cloud model.

Runs a few small real inference requests. No vehicle execution exists.
"""
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parent.parent


async def wait_task(client, tid):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        response = await client.get(f"/api/tasks/{tid}")
        response.raise_for_status()
        task = response.json()
        if task["status"] != "running":
            return task
        await asyncio.sleep(0.5)
    raise RuntimeError("本地生成任务超时")


async def main():
    results = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=20, trust_env=False) as client:
        (await client.post("/api/session")).raise_for_status()
        config = (await client.get("/api/config")).json()
        if not config["hermes_configured"]:
            raise RuntimeError("Hermes 未配置，不能执行真实模型验收")
        cases = [
            ("把空气悬架升高一档", {"control.suspension.set_height"}, None),
            ("把所有分区空调设为23度，同时把主驾座椅加热设为二档", {"control.climate.set_temperature", "control.seats.set_heating"}, None),
            ("帮我调整空气悬架", {"control.suspension.set_height"}, "升高一档"),
        ]
        for goal, expected, answer in cases:
            response = await client.post("/api/tasks", json={"goal": goal, "request_key": uuid.uuid4().hex})
            response.raise_for_status()
            tid = response.json()["id"]
            task = await wait_task(client, tid)
            entry = {"goal": goal, "initial_status": task["status"]}
            if answer:
                entry["question"] = task.get("question")
                if task["status"] != "awaiting_input":
                    entry.update({"passed": False, "status": task["status"], "summary": task.get("summary")})
                    results.append(entry)
                    continue
                (await client.post(f"/api/tasks/{tid}/answer", json={"answer": answer})).raise_for_status()
                task = await wait_task(client, tid)
            calls = task["calls"]
            passed = task["source"] == "hermes" and task["status"] == "generated" and {c["tool"] for c in calls} == expected
            entry.update({"passed": passed, "status": task["status"], "calls": calls, "executed": False, "summary": task.get("summary")})
            results.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    output = ROOT / "test-results/live-agent.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
