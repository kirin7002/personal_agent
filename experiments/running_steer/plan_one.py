"""Real-model acceptance scenarios; read-only checks over production receipts.

The exported trace deliberately excludes prompts, model prose, credentials,
request headers, configuration, and full tool response bodies.
"""
import asyncio
from collections import Counter
import json
import time
import uuid

from app.store import TERMINAL


SCENARIOS = [
    ("simple_1", "把所有分区空调设为23度", "demo-ready"),
    ("relative", "把所有分区空调温度提高1度", "demo-ready"),
    ("simple_2", "把所有分区空调设为23度", "demo-ready"),
    ("no_suspension", "把空气悬架升高一档", "demo-no-suspension"),
    ("clarification", "把空调设为23度", "demo-ready"),
    ("timer", "10秒后查询空调温度", "demo-ready"),
]


async def wait_for(predicate, timeout=150):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.025)


def receipts(store, tid):
    with store.connect() as db:
        return [{"revision": r["task_revision"], "created": r["created"],
                 "response": json.loads(r["response"])} for r in db.execute(
            "SELECT * FROM capability_calls WHERE task_id=? ORDER BY created,rowid", (tid,))]


def safe_arguments(arguments):
    result = {}
    for key, value in arguments.items():
        if key == "temperature_c" and type(value) in (int, float):
            result[key] = value
        elif key == "zone" and isinstance(value, str) and value in {"all", "driver", "passenger", "rear"}:
            result[key] = value
        elif key == "action" and isinstance(value, str) and value in {"raise", "lower", "normal"}:
            result[key] = value
        elif key == "topic" and value == "overview":
            result[key] = value
    return result


def safe_numeric_facts(data):
    # These are receipt facts, not the runtime/provider configuration.
    keys = {"driver_temperature_c", "passenger_temperature_c", "available", "air_suspension"}
    result = {}
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and type(item) in (int, float, bool):
                    result[key] = item
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(data)
    return result


def trace(store, task):
    rows = []
    for event in store.task_events(task["id"]):
        data = event["data"]
        row = {"at_ms": round((event["created"] - task["created"]) * 1000, 2), "kind": event["kind"]}
        for key in ("task_revision", "duration_ms", "started_at"):
            if type(data.get(key)) in (int, float):
                row[key] = data[key]
        if data.get("name") in {"search_capabilities", "browse_capabilities", "describe_tools", "call_tools", "request_user_input", "wait_for_event", "finish_task"}:
            row["tool"] = data["name"]
        if data.get("status") in {"running", "waiting", "awaiting_input", "generated", "completed", "unsupported", "failed", "error", "success", "prepared", "returned_mock"}:
            row["status"] = data["status"]
        rows.append(row)
    return rows


def analyze(store, task, name, elapsed_ms, extras):
    current = store.task(task["id"])
    events = store.task_events(task["id"])
    counts = Counter(e["kind"] for e in events)
    ledger = receipts(store, task["id"])
    results = [r["response"] for r in ledger]
    final = json.loads(current["final_result"]) if current.get("final_result") else {}
    final_args = [safe_arguments(c["arguments"]) for c in final.get("calls", [])]
    controls = [r for r in ledger if r["response"].get("status") == "prepared"]
    queries = [r for r in ledger if r["response"].get("status") == "returned_mock"]
    model_events = [e for e in events if e["kind"] == "agent.model_request"]
    valid_tools = set(store_catalog_ids(store, task))
    safe_receipts = [{
        "at_ms": round((r["created"] - task["created"]) * 1000, 2), "revision": r["revision"],
        "tool_id": r["response"]["tool_id"] if r["response"]["tool_id"] in valid_tools else "unknown",
        "status": r["response"]["status"], "kind": r["response"]["kind"],
        "arguments": safe_arguments(r["response"].get("arguments", {})),
        "facts": safe_numeric_facts(r["response"].get("data")),
        "error_code": (r["response"].get("error") or {}).get("code"),
    } for r in ledger]
    row = {"case": name, "actual_hermes": True, "actual_qwen": False,
           "status": current["status"], "outcome": current["outcome"], "revision": current["revision"],
           "latency_ms": round(elapsed_ms, 2), "task_elapsed_ms": round((current["updated"] - task["created"]) * 1000, 2),
           "first_provider_at_ms": round((model_events[0]["created"] - task["created"]) * 1000, 2) if model_events else None,
           "provider_requests": len(model_events), "dispatches": counts["task.dispatched"],
           "fast_completions": counts["task.fast_completed"], "confirmation_skips": counts["agent.confirmation_skipped"],
           "capability_receipts": len(results), "control_receipts": len(controls), "query_receipts": len(queries),
           "tool_steps": counts["agent.step"], "terminal_notifications": sum(e["kind"] in {"task.generated", "task.completed", "task.unsupported", "task.failed"} for e in events),
           "final_arguments": final_args, "receipts": safe_receipts, "trace": trace(store, task), **extras}
    checks = {"real_provider_called": bool(model_events), "one_session_task": extras["session_task_count"] == 1,
              "one_terminal_notification": row["terminal_notifications"] == 1}
    if name.startswith("simple"):
        checks.update(generated=current["status"] == "generated", final_all_23=final_args == [{"zone": "all", "temperature_c": 23}],
                      fast_path=counts["task.fast_completed"] == 1, one_provider_request=len(model_events) == 1,
                      one_control_no_queries=len(controls) == 1 and not queries)
    elif name == "relative":
        climate = [r for r in queries if r["response"]["tool_id"] == "status.climate.get_state"]
        baseline = climate and safe_numeric_facts(climate[0]["response"].get("data"))
        checks.update(generated=current["status"] == "generated", final_all_23=final_args == [{"zone": "all", "temperature_c": 23}],
                      queried_baseline_22=bool(baseline and baseline.get("driver_temperature_c") == 22 and baseline.get("passenger_temperature_c") == 22),
                      query_before_control=bool(climate and controls and climate[0]["created"] < controls[0]["created"]),
                      no_fast_path=counts["task.fast_completed"] == 0)
    elif name == "no_suspension":
        failed = [r for r in ledger if (r["response"].get("error") or {}).get("code") == "NOT_EQUIPPED"]
        diagnostics = [r for r in queries if r["response"]["kind"] in {"status", "manual"}]
        checks.update(unsupported=current["status"] == "unsupported", not_equipped_receipt=bool(failed),
                      diagnostics_after_failure=bool(failed and any(r["created"] > failed[0]["created"] for r in diagnostics)),
                      no_control_success=not controls and not final_args, no_fast_path=counts["task.fast_completed"] == 0)
    elif name == "clarification":
        checks.update(generated=current["status"] == "generated", final_all_23=final_args == [{"zone": "all", "temperature_c": 23}],
                      asked_before_control=extras.get("asked_before_control") is True,
                      same_task_after_answer=extras.get("same_task_after_answer") is True,
                      resumed_revision=current["revision"] == 1, one_question=counts["task.question"] == 1)
    elif name == "timer":
        waits = [e for e in events if e["kind"] == "task.waiting"]
        resumed = [e for e in events if e["kind"] == "task.resumed"]
        start = waits[0]["created"] if waits else None
        end = resumed[0]["created"] if resumed else None
        row["provider_requests_during_wait"] = sum(start <= e["created"] < end for e in model_events) if start and end else None
        row["capability_receipts_during_wait"] = sum(start <= r["created"] < end for r in ledger) if start and end else None
        row["actual_wait_ms"] = round((end-start)*1000, 2) if start and end else None
        checks.update(completed_query=current["status"] == "completed", initial_waiting=extras.get("initial_waiting") is True,
                      waited_at_least_10s=bool(end and end >= task["created"] + 10),
                      no_provider_during_wait=row["provider_requests_during_wait"] == 0,
                      no_receipts_during_wait=row["capability_receipts_during_wait"] == 0,
                      one_wait_one_resume=len(waits) == 1 and len(resumed) == 1,
                      same_task_revision_one=current["revision"] == 1,
                      climate_query=any(r["response"]["tool_id"] == "status.climate.get_state" for r in queries),
                      no_control=not controls)
    row["checks"] = checks
    row["passed"] = all(checks.values())
    return row


def store_catalog_ids(store, task):
    from app.capability_catalog import CapabilityCatalog
    return CapabilityCatalog(store, initialize=False).snapshot(task["catalog_revision"]).tools


async def run_case(app, client, name, goal, profile):
    store, manager = app.state.store, app.state.manager
    client.cookies.clear()
    (await client.post("/api/session")).raise_for_status()
    sid = client.cookies["preview_session"]
    started = time.monotonic()
    response = await client.post("/api/tasks", json={"goal": goal, "request_key": uuid.uuid4().hex,
                                  "profile_id": profile, "discovery_mode": "auto", "completion_mode": "auto"})
    response.raise_for_status()
    task = store.task(response.json()["id"])
    extras = {}
    try:
        if name == "clarification":
            await wait_for(lambda: store.task(task["id"])["status"] != "running")
            asked = store.task(task["id"])
            extras["asked_before_control"] = asked["status"] == "awaiting_input" and not receipts(store, task["id"])
            if asked["status"] == "awaiting_input":
                job = manager.jobs.get(task["id"])
                if job:
                    await asyncio.wait_for(asyncio.shield(job), 15)
                answer = await client.post(f"/api/tasks/{task['id']}/answer", json={"answer": "全部", "task_revision": asked["revision"]})
                answer.raise_for_status()
                extras["same_task_after_answer"] = answer.json()["id"] == task["id"]
        if name == "timer":
            extras["initial_waiting"] = task["status"] == "waiting"
        await wait_for(lambda: store.task(task["id"])["status"] in TERMINAL)
        job = manager.jobs.get(task["id"])
        if job:
            await asyncio.wait_for(asyncio.shield(job), 15)
        extras["session_task_count"] = len(store.tasks(sid))
        return analyze(store, task, name, (time.monotonic()-started)*1000, extras)
    except Exception as exc:
        extras.update(session_task_count=len(store.tasks(sid)), error_type=type(exc).__name__)
        row = analyze(store, task, name, (time.monotonic()-started)*1000, extras)
        row["passed"] = False
        return row
    finally:
        current = store.task(task["id"])
        if current["status"] not in TERMINAL:
            await manager.cancel(sid, task["id"])
        else:
            await manager.stop_remote(current["run_id"])
