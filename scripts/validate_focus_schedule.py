"""Opt-in sections 15/16 checks on fresh private application/Hermes instances.

No real recurring-user plans or existing services are touched. Any plans created
by this harness live only in its private database and are cancelled in cleanup.
Run after production contracts are ready; --qwen-live uses actual upstream Qwen
with text ingress and audio output, not a physical microphone.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid

import httpx
from dotenv import dotenv_values
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.main import create_app
from app.realtime import VoiceBridge
from scripts.validate_running_steer import private_write, bound_socket, wait_for, LiveBrowser


@asynccontextmanager
async def isolated():
    folder = ROOT / ".runtime" / ("focus-schedule-" + uuid.uuid4().hex[:10])
    folder.mkdir(mode=0o700)
    home = folder / "home"
    home.mkdir(mode=0o700)
    source = ROOT / ".runtime/hermes-home"
    repo = ROOT / ".runtime/hermes-agent"
    gateway_socket, app_socket = bound_socket(), bound_socket()
    gateway_port, app_port = gateway_socket.getsockname()[1], app_socket.getsockname()[1]
    key = secrets.token_urlsafe(32)
    config = json.loads((source / "config.yaml").read_text())
    config["gateway"]["api_server"].update(host="127.0.0.1", port=gateway_port, max_concurrent_runs=1)
    config["plugins"]["enabled"] = ["bailian-limits", "vehicle-completion"]
    db_path = folder / "tasks.sqlite3"
    config["mcp_servers"]["vehicle_preview"].update(args=["-m", "app.mcp_server"], env={"PREVIEW_DB": str(db_path)}, timeout=60)
    private_write(home / "config.yaml", json.dumps(config, ensure_ascii=False))
    source_env = {k: v for k, v in dotenv_values(source / ".env").items() if v is not None}
    override = {"API_SERVER_KEY": key, "API_SERVER_ENABLED": "true", "API_SERVER_PORT": str(gateway_port), "API_SERVER_HOST": "127.0.0.1"}
    private_write(home / ".env", "\n".join(f"{k}={json.dumps(v)}" for k, v in {**source_env, **override}.items()) + "\n")
    for source_name, installed_name in [("bailian_limits", "bailian-limits"), ("vehicle_completion", "vehicle-completion")]:
        shutil.copytree(ROOT / "hermes_plugins" / source_name, home / "plugins" / installed_name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    env = {**os.environ, **source_env, **override, "HERMES_HOME": str(home), "PREVIEW_DB": str(db_path),
           "HERMES_RUNTIME_DIR": str(ROOT / ".runtime/hermes-tools"), "PYTHONPATH": str(ROOT)}
    env["NO_PROXY"] = env.get("NO_PROXY", "") + ",localhost,127.0.0.1,::1,dashscope.aliyuncs.com"
    env["no_proxy"] = env["NO_PROXY"]
    os.environ["HERMES_HOME"] = str(source)
    os.environ["HERMES_RUNTIME_DIR"] = str(ROOT / ".runtime/hermes-tools")
    sys.path.insert(0, str(repo))
    from pm.environments import project_python, runtime_facts_path
    python, facts = project_python(repo), runtime_facts_path(repo)
    local_facts = home / facts.relative_to(source)
    private_write(local_facts, facts.read_text())
    (local_facts.parent / "environments").symlink_to(facts.parent / "environments", target_is_directory=True)
    settings = Settings(db_path=db_path, hermes_url=f"http://127.0.0.1:{gateway_port}", hermes_key=key, task_timeout=150)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=app_port, access_log=False, log_level="error"))
    app_job = asyncio.create_task(server.serve(sockets=[app_socket]))
    process = None
    try:
        await wait_for(lambda: server.started or app_job.done(), 15)
        if app_job.done():
            raise RuntimeError("Isolated application did not start")
        gateway_socket.close()
        fd = os.open(folder / "gateway.log", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "wb", buffering=0) as log:
            process = subprocess.Popen([str(python), "-m", "gateway.run"], cwd=repo, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        async with httpx.AsyncClient(base_url=settings.hermes_url, headers={"Authorization": f"Bearer {key}"}, trust_env=False, timeout=3) as gateway:
            for _ in range(100):
                try:
                    result = await gateway.get("/v1/capabilities")
                    result.raise_for_status()
                    if result.json().get("features", {}).get("run_submission"):
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Isolated gateway ended")
                await asyncio.sleep(.3)
            else:
                raise TimeoutError("Isolated gateway did not become ready")
        print(json.dumps({"stage": "ready", "app_port": app_port, "gateway_port": gateway_port}), flush=True)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{app_port}", trust_env=False, timeout=25) as client:
            yield app, client, settings, {"app_port": app_port, "gateway_port": gateway_port}
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(app_job, 20)
        finally:
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.to_thread(process.wait, 8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    await asyncio.to_thread(process.wait)
            gateway_socket.close()
            app_socket.close()
        print("Isolated focus/schedule services stopped; existing services untouched.", flush=True)


async def conversation(client, text, **context):
    response = await client.post("/api/conversation/input", json={"text": text, "event_id": uuid.uuid4().hex, **context})
    response.raise_for_status()
    return response.json()


def evidence(store, tid):
    task = store.task(tid)
    events = store.task_events(tid)
    final = json.loads(task["final_result"]) if task.get("final_result") else {}
    terminal = [e["created"] for e in events if e["kind"] in {"task.cancelled", "task.expired", "task.generated", "task.completed"}]
    waiting = False
    provider_while_waiting = 0
    for event in events:
        if event["kind"] in {"task.question", "task.waiting"}:
            waiting = True
        elif event["kind"] in {"task.resumed", "task.cancelled", "task.expired", "task.generated", "task.completed"}:
            waiting = False
        elif event["kind"] == "agent.model_request" and waiting:
            provider_while_waiting += 1
    return {"status": task["status"], "revision": task["revision"],
        "provider_requests": sum(e["kind"] == "agent.model_request" for e in events),
        "provider_requests_while_waiting": provider_while_waiting,
        "local_wait_closures": sum(e["kind"] == "agent.wait_confirmation_skipped" for e in events),
        "provider_requests_after_terminal": sum(e["kind"] == "agent.model_request" and bool(terminal) and e["created"] > min(terminal) for e in events),
        "receipt_count": len(store.capability_results(tid)),
        "final_arguments": [c["arguments"] for c in final.get("calls", [])],
        "trace": [{"kind": e["kind"], "at_ms": round((e["created"]-task["created"])*1000, 2)} for e in events]}


async def http_question_case(app, client, answer):
    client.cookies.clear()
    (await client.post("/api/session")).raise_for_status()
    store = app.state.store
    result = await conversation(client, "把空调设为23度")
    tid = result["task"]["id"]
    await wait_for(lambda: store.task(tid)["status"] != "running", 90)
    asked = store.task(tid)
    no_receipt = not store.capability_results(tid)
    if asked["status"] != "awaiting_input":
        raise AssertionError("Expected actual model/production guard to ask for zone")
    # Allow the original production monitor/native run to close before answering.
    pending_job = app.state.manager.jobs.get(tid)
    if pending_job:
        await asyncio.wait_for(asyncio.shield(pending_job), 15)
    followup = await conversation(client, answer)
    await wait_for(lambda: store.task(tid)["status"] in {"cancelled", "generated", "failed", "expired"}, 90)
    job = app.state.manager.jobs.get(tid)
    if job:
        await asyncio.wait_for(asyncio.shield(job), 15)
    row = evidence(store, tid)
    row.update(case="http_cancel" if answer == "算了" else "http_answer", actual_hermes=True,
               asked_without_receipt=no_receipt, same_task=followup.get("task", {}).get("id") == tid,
               session_tasks=len(store.tasks(client.cookies["preview_session"])))
    expected = "cancelled" if answer == "算了" else "generated"
    row["passed"] = (row["status"] == expected and row["same_task"] and no_receipt and row["session_tasks"] == 1
                     and row["provider_requests_while_waiting"] == 0 and row["local_wait_closures"] >= 1)
    if answer == "算了":
        row["passed"] &= row["receipt_count"] == 0 and row["provider_requests_after_terminal"] == 0
    else:
        row["passed"] &= row["final_arguments"] == [{"zone": "all", "temperature_c": 23}]
    return row


async def plan_dialogue_case(app, client):
    client.cookies.clear()
    (await client.post("/api/session")).raise_for_status()
    sid = client.cookies["preview_session"]
    result = await conversation(client, "每天早上查询空调温度")
    missing_time = not result.get("plan")
    # Select a future local wall-clock minute; never trigger a real-user plan.
    future = (datetime.now().astimezone() + timedelta(hours=2)).strftime("%H:%M")
    completed = await conversation(client, future)
    plan = completed["plan"]
    operations = []
    try:
        for operation in ("pause", "resume", "cancel_plan"):
            response = await client.post(f"/api/plans/{plan['id']}/action", json={"action": operation, "revision": plan["revision"]})
            response.raise_for_status()
            result = response.json()
            plan = result.get("plan", result)
            operations.append({"action": operation, "status": plan["status"], "revision": plan["revision"]})
        return {"case": "daily_missing_time_management", "missing_time_clarified": missing_time,
                "actions": operations, "session_tasks": len(app.state.store.tasks(sid)),
                "passed": missing_time and not app.state.store.tasks(sid) and plan["status"] in {"cancelled", "canceled"}}
    finally:
        if plan.get("status") not in {"cancelled", "canceled"}:
            await client.post(f"/api/plans/{plan['id']}/action", json={"action": "cancel_plan", "revision": plan["revision"]})


async def qwen_case(app, settings):
    store, manager = app.state.store, app.state.manager
    sid = store.create_session()
    browser = LiveBrowser()
    bridge = VoiceBridge(browser, sid, settings, store, manager)
    browser.bridge = bridge
    routed = []
    production_callback = bridge.handle_tool
    async def observe_callback(event):
        # Observe the actual model-selected envelope, then call production code
        # unchanged. Never supply, remove, or rewrite task identifiers.
        if event.get("name") == "route_vehicle_input":
            arguments = json.loads(event.get("arguments", "{}"))
            routed.append({"has_task_id": "task_id" in arguments,
                           "has_task_revision": "task_revision" in arguments,
                           "raw_text_matches_input": arguments.get("text") in {"把空调设为23度", "算了"}})
        await production_callback(event)
    bridge.handle_tool = observe_callback
    worker = asyncio.create_task(bridge.run())
    try:
        await wait_for(lambda: bridge.gate.ready or worker.done(), 30)
        if worker.done():
            raise RuntimeError("Actual Qwen connection did not become ready")
        await browser.incoming.put({"type": "text", "text": "把空调设为23度"})
        await wait_for(lambda: bool(store.tasks(sid)), 40)
        tid = store.tasks(sid)[0]["id"]
        await wait_for(lambda: store.task(tid)["status"] == "awaiting_input", 90)
        await wait_for(lambda: bridge.gate.can_send(), 40)
        await browser.incoming.put({"type": "text", "text": "算了"})
        await wait_for(lambda: store.task(tid)["status"] == "cancelled", 40)
        await wait_for(lambda: bridge.gate.can_send(), 40)
        row = evidence(store, tid)
        row.update(case="actual_qwen_shared_route_cancel", actual_qwen=True, input_modality="text",
                   audio_chunks=browser.audio_chunks, session_tasks=len(store.tasks(sid)), route_callbacks=routed)
        row["passed"] = (row["status"] == "cancelled" and row["session_tasks"] == 1
                         and row["receipt_count"] == 0 and browser.audio_chunks > 0 and len(routed) >= 2
                         and row["provider_requests_while_waiting"] == 0 and row["local_wait_closures"] >= 1
                         and all(c["raw_text_matches_input"] and not c["has_task_id"] and not c["has_task_revision"] for c in routed))
        return row
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def recurring_execution_case(app, client):
    store, manager = app.state.store, app.state.manager
    # Time acceleration is explicitly confined to this harness's private DB.
    if not store.path.resolve().parent.name.startswith("focus-schedule-"):
        raise AssertionError("Refusing to alter a non-harness plan timestamp")
    client.cookies.clear()
    (await client.post("/api/session")).raise_for_status()
    sid = client.cookies["preview_session"]
    future = (datetime.now().astimezone() + timedelta(hours=2)).strftime("%H:%M")
    response = await client.post("/api/plans", json={"goal": "查询空调温度", "request_key": uuid.uuid4().hex,
        "schedule": {"frequency": "daily", "time": future, "weekdays": []}})
    response.raise_for_status()
    plan = response.json()["plan"]
    foreground = None
    try:
        initial = await conversation(client, "把空调设为23度")
        foreground = initial["task"]["id"]
        await wait_for(lambda: store.task(foreground)["status"] != "running", 90)
        if store.task(foreground)["status"] != "awaiting_input":
            raise AssertionError("Foreground question was not established")
        job = manager.jobs.get(foreground)
        if job:
            await asyncio.wait_for(asyncio.shield(job), 15)
        focus_before = manager.router.current(sid)["id"]
        forced_due = time.time() - .1
        with store.connect() as db:
            db.execute("UPDATE task_plans SET next_run_at=? WHERE id=? AND session_id=?",
                       (forced_due, plan["id"], sid))
        await manager.dispatch_plans()
        runs = manager.plans.runs(sid, plan["id"])
        if len(runs) != 1 or not runs[0].get("task_id"):
            raise AssertionError("Due plan did not create exactly one occurrence")
        tid = runs[0]["task_id"]
        await wait_for(lambda: store.task(tid)["status"] not in {"running", "waiting"}, 120)
        job = manager.jobs.get(tid)
        if job:
            await asyncio.wait_for(asyncio.shield(job), 15)
        await manager.dispatch_plans()  # Repeat dispatch must not duplicate it.
        runs = manager.plans.runs(sid, plan["id"])
        results = store.capability_results(tid)
        queries = [r for r in results if r.get("tool_id") == "status.climate.get_state" and r.get("status") == "returned_mock"]
        focus_after = manager.router.current(sid)
        row = evidence(store, tid)
        row.update(case="recurring_due_hermes_query", actual_hermes=True,
                   isolated_due_timestamp_forced=True, plan_runs=len(runs),
                   current_focus_unchanged=bool(focus_after and focus_after["id"] == focus_before == foreground),
                   foreground_status=store.task(foreground)["status"],
                   climate_query_receipts=len(queries),
                   plan_pending=manager.plans.get(sid, plan["id"])["status"] == "pending",
                   foreground_wait_closures=evidence(store, foreground)["local_wait_closures"])
        row["passed"] = (row["status"] == "completed" and row["provider_requests"] > 0
                         and row["provider_requests_after_terminal"] == 0 and row["plan_runs"] == 1
                         and row["current_focus_unchanged"] and row["foreground_status"] == "awaiting_input"
                         and row["climate_query_receipts"] >= 1 and not row["final_arguments"] and row["plan_pending"])
        return row
    finally:
        current = manager.plans.get(sid, plan["id"])
        if current["status"] != "cancelled":
            await manager.plan_action(sid, plan["id"], "cancel_plan", current["revision"])
        if foreground and store.task(foreground)["status"] in {"running", "waiting", "awaiting_input"}:
            await manager.cancel(sid, foreground)


async def qwen_plan_management_case(app, settings):
    """Six real Qwen turns; no direct application mutation drives the dialogue."""
    store, manager = app.state.store, app.state.manager
    sid = store.create_session()
    browser = LiveBrowser()
    bridge = VoiceBridge(browser, sid, settings, store, manager)
    browser.bridge = bridge
    callbacks, steps = [], []
    active_input = {"text": None}
    production_callback = bridge.handle_tool
    async def observe_callback(event):
        if event.get("name") == "route_vehicle_input":
            arguments = json.loads(event.get("arguments", "{}"))
            callbacks.append({"step": len(steps) + 1,
                              "raw_text_matches_input": arguments.get("text") == active_input["text"],
                              "has_task_id": "task_id" in arguments,
                              "has_task_revision": "task_revision" in arguments})
        await production_callback(event)
    bridge.handle_tool = observe_callback
    worker = asyncio.create_task(bridge.run())
    row = {"case": "actual_qwen_plan_management", "actual_qwen": True, "input_modality": "text",
           "physical_microphone_tested": False, "steps": steps, "route_callbacks": callbacks}

    def plan_state():
        plans = manager.plans.list(sid)
        return plans[0] if len(plans) == 1 else None

    def dialog_kind():
        dialog = manager.dialog(sid)
        return dialog.get("kind") if dialog else None

    def quiet():
        return (bridge.gate.can_send() and not bridge.pending_tool_reply and not bridge.tasks
                and bridge.inflight is None and not manager.plans.notifications(sid))

    dialogue = [
        ("每天早上查询空调温度", "schedule_creation", lambda: dialog_kind() == "schedule_creation" and plan_state() is None),
        ("早上8点", "created", lambda: bool(plan_state() and plan_state()["status"] == "pending")),
        ("暂停这个计划", "paused", lambda: bool(plan_state() and plan_state()["status"] == "paused")),
        ("恢复这个计划", "resumed", lambda: bool(plan_state() and plan_state()["status"] == "pending" and plan_state()["revision"] >= 2)),
        ("取消这个计划", "cancel_scope", lambda: dialog_kind() == "cancel_scope"),
        ("整个计划", "cancelled", lambda: bool(plan_state() and plan_state()["status"] == "cancelled")),
    ]
    try:
        await wait_for(lambda: bridge.gate.ready or worker.done(), 30)
        if worker.done():
            raise RuntimeError("Actual Qwen connection did not become ready")
        for utterance, expected, changed in dialogue:
            await wait_for(quiet, 45)
            active_input["text"] = utterance
            audio_before, callbacks_before = browser.audio_chunks, len(callbacks)
            await browser.incoming.put({"type": "text", "text": utterance})
            await wait_for(lambda: changed() or worker.done(), 45)
            if worker.done():
                raise RuntimeError("Qwen connection ended during plan dialogue")
            await wait_for(lambda: browser.audio_chunks > audio_before and quiet(), 45)
            plan = plan_state()
            dialog = manager.dialog(sid)
            step = {"input": utterance, "expected": expected,
                    "plan_status": plan["status"] if plan else None,
                    "plan_revision": plan["revision"] if plan else None,
                    "dialog_kind": dialog.get("kind") if dialog else None,
                    "audio_chunks": browser.audio_chunks-audio_before,
                    "route_calls": len(callbacks)-callbacks_before,
                    "business_tasks": len(store.tasks(sid))}
            if plan:
                step["schedule"] = {k: plan["schedule"][k] for k in ("frequency", "time", "timezone")}
                if expected == "created" and plan["next_run_at"] < time.time() + 120:
                    raise RuntimeError("Scheduled time is too near for an isolated no-execution smoke")
            if expected == "cancel_scope":
                question = dialog.get("question", "") if dialog else ""
                step["scope_question_has_once_and_whole"] = "一次" in question and "计划" in question
            steps.append(step)
            print(json.dumps({"stage": "qwen_plan_step", **step}, ensure_ascii=False), flush=True)
        plans = manager.plans.list(sid)
        row.update(audio_chunks=browser.audio_chunks, business_tasks=len(store.tasks(sid)),
                   plan_count=len(plans), active_plans=sum(p["status"] != "cancelled" for p in plans),
                   business_provider_requests=sum(e["kind"] == "agent.model_request" for e in store.events(sid)),
                   runtime_timezone=manager.plans.timezone)
        row["passed"] = (
            len(steps) == 6 and row["plan_count"] == 1 and row["active_plans"] == 0
            and row["business_tasks"] == 0 and row["business_provider_requests"] == 0
            and all(s["audio_chunks"] > 0 and s["route_calls"] >= 1 and s["business_tasks"] == 0 for s in steps)
            and all(c["raw_text_matches_input"] and not c["has_task_id"] and not c["has_task_revision"] for c in callbacks)
            and steps[4].get("scope_question_has_once_and_whole") is True
            and all(s.get("schedule", {}).get("time", "08:00") == "08:00" for s in steps)
            and plans[0]["schedule"]["timezone"] == manager.plans.timezone
        )
    except Exception as exc:
        row.update(passed=False, error_type=type(exc).__name__, failed_step=len(steps)+1,
                   audio_chunks=browser.audio_chunks, business_tasks=len(store.tasks(sid)))
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for plan in manager.plans.list(sid):
            if plan["status"] != "cancelled":
                await manager.plan_action(sid, plan["id"], "cancel_plan", plan["revision"])
        row["cleanup_active_plans"] = sum(p["status"] != "cancelled" for p in manager.plans.list(sid))
    return row


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-live", action="store_true")
    parser.add_argument("--cases", default="", help="Optional comma-separated case names; Qwen requires --qwen-live")
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/focus-schedule-live.json")
    args = parser.parse_args()
    rows = []
    async with isolated() as (app, client, settings, ports):
        jobs = [("http_cancel", lambda: http_question_case(app, client, "算了")),
                ("http_answer", lambda: http_question_case(app, client, "全部")),
                ("daily_missing_time_management", lambda: plan_dialogue_case(app, client)),
                ("recurring_due_hermes_query", lambda: recurring_execution_case(app, client))]
        if args.qwen_live:
            jobs.append(("actual_qwen_shared_route_cancel", lambda: qwen_case(app, settings)))
            jobs.append(("actual_qwen_plan_management", lambda: qwen_plan_management_case(app, settings)))
        if args.cases:
            selected = set(args.cases.split(","))
            if selected - {name for name, _ in jobs}:
                raise ValueError("Unknown or disabled live case")
            jobs = [(name, run) for name, run in jobs if name in selected]
        for name, run in jobs:
            started = time.monotonic()
            try:
                row = await run()
            except Exception as exc:
                row = {"case": name, "passed": False, "error_type": type(exc).__name__}
            row["latency_ms"] = round((time.monotonic()-started)*1000, 2)
            rows.append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"isolated": True, **ports, "cases": rows}, ensure_ascii=False, indent=2)+"\n")
            print(json.dumps({k: v for k, v in row.items() if k != "trace"}, ensure_ascii=False), flush=True)
    return 0 if rows and all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}), flush=True)
        raise SystemExit(2)
