"""Evaluate the real Hermes directory workflow; mock business data, real model decisions."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parent.parent


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="C001,C011,C017,C025,C033,C040,C044,C047,Q001,Q002,X001,X002,X003,X004")
    parser.add_argument("--all-controls", action="store_true")
    parser.add_argument("--concurrency", type=int, default=2, choices=[1, 2])
    args = parser.parse_args()
    results = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=20, trust_env=False) as client:
        (await client.post("/api/session")).raise_for_status()
        metadata = (await client.get("/api/catalog")).json()
        ids = set(args.ids.split(","))
        cases = [s for s in metadata["scenarios"] if (s["kind"] == "control" if args.all_controls else s["id"] in ids)]
        if not cases:
            raise RuntimeError("未找到测试场景")
        semaphore = asyncio.Semaphore(args.concurrency)
        output = ROOT / "test-results" / ("live-catalog-controls-50.json" if args.all_controls else "live-catalog.json")
        output.parent.mkdir(exist_ok=True)
        async def run(scenario):
            async with semaphore:
                start = time.monotonic()
                r = await client.post("/api/tasks", json={"goal": scenario["instruction"], "profile_id": scenario["profile_id"], "request_key": uuid.uuid4().hex})
                r.raise_for_status()
                tid = r.json()["id"]
                task = r.json()
                while time.monotonic() - start < 150:
                    task = (await client.get(f"/api/tasks/{tid}")).json()
                    if task["status"] != "running":
                        break
                    await asyncio.sleep(0.6)
                expected = scenario["expected"]
                outcome_ok = task.get("outcome") == expected["outcome"] or (expected["outcome"] == "prepared" and task.get("outcome") == "mixed")
                actual = [{"tool_id": c["tool"], "arguments": c["arguments"]} for c in task["calls"]]
                receipts = task.get("results", [])
                # A diagnostic fallback may truthfully finish as partial: the
                # diagnosis is done, but the requested control remains blocked.
                equivalent_partial = expected["outcome"] == "unsupported" and task.get("outcome") == "partial" and not actual and bool(task.get("output", {}).get("unresolved")) and any(r["status"] == "error" for r in receipts)
                outcome_ok = outcome_ok or equivalent_partial
                successes = [{"tool_id": r["tool_id"], "arguments": r["arguments"]} for r in receipts if r["status"] in {"prepared", "returned_mock"}]
                expected_controls = [c for c in expected["calls"] if c["tool_id"].startswith("control.")]
                controls_ok = all(c in successes for c in expected["calls"]) and len(actual) == len(expected_controls)
                domains = {r["kind"] for r in receipts if r.get("kind")}
                # Discovery of a control plus evidence that it cannot run need not issue a doomed command.
                activity = (await client.get("/api/events")).json()["events"]
                for event in activity:
                    if event["task_id"] == tid and event["kind"] == "catalog.described":
                        domains |= {t.split(".")[0] for t in event["data"]["tool_ids"]}
                domains_ok = set(expected["required_domains"]) <= domains
                record = {"id": scenario["id"], "goal": scenario["instruction"], "passed": outcome_ok and controls_ok and domains_ok, "resolution_match": "diagnosis_complete_control_blocked" if equivalent_partial else "exact_or_additional_queries", "status": task["status"], "outcome": task.get("outcome"), "calls": actual, "domains": sorted(domains), "elapsed_seconds": round(time.monotonic()-start, 2), "summary": task.get("summary"), "question": task.get("question")}
                results.append(record)
                output.write_text(json.dumps(sorted(results, key=lambda x: x["id"]), ensure_ascii=False, indent=2)+"\n")
                print(json.dumps(record, ensure_ascii=False), flush=True)
                if task["status"] in {"running", "awaiting_input"}:
                    await client.post(f"/api/tasks/{tid}/cancel")
        await asyncio.gather(*(run(case) for case in cases))
    print(json.dumps({"passed": sum(r["passed"] for r in results), "total": len(results)}, ensure_ascii=False))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
