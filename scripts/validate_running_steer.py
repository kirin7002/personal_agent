"""Opt-in real Hermes validation using production task, tool, and completion code.

Run: .venv/bin/python scripts/validate_running_steer.py --qwen-live
Only new private runtime files and the requested report are written. Existing
services, their config, and their task databases are never changed.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
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
from experiments.running_steer import barriers
from experiments.running_steer.voice import capture_bridge, generated_notification, revise_callback


def private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    path.chmod(0o600)


def bound_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


async def wait_for(predicate, timeout=100):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


def summarize(store, task, run_id, changed_at, window):
    current = store.task(task["id"])
    events = store.task_events(task["id"])
    timing = barriers.state(store, task["id"])
    with store.connect() as db:
        receipts = [{"revision": r["task_revision"], "created": r["created"],
                     "response": json.loads(r["response"])} for r in db.execute(
                         "SELECT * FROM capability_calls WHERE task_id=? ORDER BY created", (task["id"],))]
    prepared = [r for r in receipts if r["response"].get("status") == "prepared"]
    stale_after = [r for r in prepared if r["created"] >= changed_at and
                   r["response"].get("arguments", {}).get("temperature_c") != 21]
    old_before = [r for r in prepared if r["created"] < changed_at and
                  r["response"].get("arguments", {}).get("temperature_c") == 23]
    final = json.loads(current["final_result"]) if current.get("final_result") else {}
    arguments = [c["arguments"] for c in final.get("calls", [])]
    change = store.latest_change(task["id"])
    in_flight = bool(timing["provider_started"] and timing["provider_returned"] and
                     timing["provider_started"] <= changed_at <= timing["provider_returned"])
    row = {
        "window": window, "status": current["status"], "revision": current["revision"],
        "same_task_and_run": current["run_id"] == run_id,
        "dispatches": sum(e["kind"] == "task.dispatched" for e in events),
        "change_status": change["status"] if change else None,
        "final_arguments": arguments,
        "old_receipts_before_change": len(old_before), "stale_receipts_after_change": len(stale_after),
        "completed_notifications": sum(e["kind"] == "task.generated" for e in events),
        "provider_calls": sum(e["kind"] == "agent.model_request" for e in events),
        "production_confirmation_skips": sum(e["kind"] == "agent.confirmation_skipped" for e in events),
        "change_during_provider_invocation": in_flight,
        "tool_errors": [e["data"]["error_code"] for e in events if e["kind"] == "validation.tool" and e["data"].get("error_code")],
    }
    row["passed"] = (row["status"] == "generated" and row["revision"] == 1
                     and row["same_task_and_run"] and row["dispatches"] == 1
                     and row["change_status"] in {"accepted", "applied"}
                     and arguments == [{"zone": "all", "temperature_c": 21}]
                     and not stale_after and row["completed_notifications"] == 1
                     and row["provider_calls"] > 0 and row["production_confirmation_skips"] >= 1
                     and (window != "before_finish" or len(old_before) == 1)
                     and (window != "during_model" or in_flight))
    return row


async def run_case(app, client, settings, entry, window):
    store, manager = app.state.store, app.state.manager
    await client.post("/api/session")
    sid = client.cookies["preview_session"]
    # Register synchronously before the newly created production job gets CPU.
    task = manager.submit(sid, "把所有分区空调设为23度", uuid.uuid4().hex,
                          completion_mode="agent" if window == "before_finish" else "auto")
    barriers.register(store, task["id"], window)
    bridge, sent = capture_bridge(sid, settings, store, manager)
    run_id = None
    try:
        await barriers.wait_reached(store, task["id"])
        await wait_for(lambda: bool(store.task(task["id"])["run_id"]))
        run_id = store.task(task["id"])["run_id"]
        if entry == "http":
            body = {"text": "改成21度", "task_revision": 0, "change_id": uuid.uuid4().hex}
            response = await client.post(f"/api/tasks/{task['id']}/revise", json=body)
            response.raise_for_status()
            duplicate = await client.post(f"/api/tasks/{task['id']}/revise", json=body)
            duplicate.raise_for_status()
        else:
            await revise_callback(bridge, sent, task["id"], 0, "改成21度")
        change = store.latest_change(task["id"])
        if not change:
            raise AssertionError("Production task change was not persisted")
        changed_at = change["created"]
        barriers.mark(store, task["id"], "released")
        await wait_for(lambda: store.task(task["id"])["status"] != "running", 150)
        job = manager.jobs.get(task["id"])
        if job:
            await asyncio.wait_for(asyncio.shield(job), 15)
        row = summarize(store, task, run_id, changed_at, window)
        row["entry"] = entry
        public = (await client.get(f"/api/tasks/{task['id']}")).json()
        row["http_projection_current"] = public.get("revision") == 1 and public.get("calls") == store.task(task["id"])["calls"]
        if store.task(task["id"])["status"] == "generated":
            notice = await generated_notification(bridge, sent)
            row["voice_callback_notice_current"] = notice["result"]["calls"][0]["arguments"] == {"zone": "all", "temperature_c": 21}
        else:
            row["voice_callback_notice_current"] = False
        row["actual_qwen"] = False
        row["passed"] &= row["http_projection_current"] and row["voice_callback_notice_current"]
        return row
    finally:
        barriers.mark(store, task["id"], "released")
        manager.change_listeners.discard(bridge.invalidate_task)
        await manager.stop_remote(run_id or store.task(task["id"])["run_id"])


class LiveBrowser:
    """Text ingress and audio observation, with no replacement of Qwen callbacks."""
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.messages = []
        self.audio_chunks = 0
        self.notification_audio_chunks = 0
        self.bridge = None

    async def receive_json(self):
        return await self.incoming.get()

    async def send_json(self, event):
        if event.get("type") == "audio":
            self.audio_chunks += 1
            if self.bridge and self.bridge.response_notices.get(self.bridge.current_response) is not None:
                self.notification_audio_chunks += 1
        else:
            self.messages.append(event)

    async def close(self, **kwargs):
        pass


async def run_qwen(app, settings, audio_clips=None):
    if not settings.audio_ready:
        return {"entry": "qwen_live_text", "passed": False, "error": "audio_not_configured", "actual_qwen": False}
    store, manager = app.state.store, app.state.manager
    sid = store.create_session()
    browser = LiveBrowser()
    bridge = VoiceBridge(browser, sid, settings, store, manager)
    browser.bridge = bridge
    worker = asyncio.create_task(bridge.run())
    task = None
    try:
        await wait_for(lambda: bridge.gate.ready or worker.done(), 30)
        if worker.done():
            raise RuntimeError("Qwen connection ended before readiness")
        # Arm all tasks created by this isolated voice session before Hermes can
        # issue an MCP call. SQLite registration itself adds no task protection.
        async def arm():
            await wait_for(lambda: bool(store.tasks(sid)), 45)
            created = store.tasks(sid)[0]
            barriers.register(store, created["id"], "before_control")
        armer = asyncio.create_task(arm())
        if audio_clips:
            from experiments.running_steer.audio import send_pcm
            await send_pcm(browser, audio_clips[0])
        else:
            await browser.incoming.put({"type": "text", "text": "把所有分区空调设为23度"})
        try:
            await armer
        finally:
            if not armer.done():
                armer.cancel()
                await asyncio.gather(armer, return_exceptions=True)
        task = store.tasks(sid)[0]
        await barriers.wait_reached(store, task["id"], 90)
        if audio_clips:
            await wait_for(lambda: any(m.get("type") == "user.transcript" for m in browser.messages), 15)
        await wait_for(lambda: bridge.gate.can_send(), 35)
        run_id = store.task(task["id"])["run_id"]
        if audio_clips:
            await send_pcm(browser, audio_clips[1])
        else:
            await browser.incoming.put({"type": "text", "text": "改成21度"})
        await wait_for(lambda: store.task(task["id"])["revision"] > 0, 35)
        change = store.latest_change(task["id"])
        barriers.mark(store, task["id"], "released")
        await wait_for(lambda: store.task(task["id"])["status"] != "running", 150)
        await wait_for(lambda: any(e["kind"] == "task.generated" and e["spoken"] for e in store.events(sid)), 60)
        row = summarize(store, task, run_id, change["created"], "before_control")
        row.update(entry="qwen_live_prerecorded_audio" if audio_clips else "qwen_live_text", actual_qwen=True,
                   input_modality="prerecorded_speech" if audio_clips else "text", physical_microphone_tested=False,
                   qwen_audio_chunks=browser.audio_chunks, session_tasks=len(store.tasks(sid)),
                   notification_audio_chunks=browser.notification_audio_chunks,
                   notification_acknowledged=True)
        if audio_clips:
            row["asr_transcripts"] = [m.get("text", "")[:300] for m in browser.messages if m.get("type") == "user.transcript"]
            row["input_audio_seconds"] = [round(len(pcm)/32000, 3) for pcm in audio_clips]
            row["correction_source"] = "local_macos_Tingting_TTS"
            row["passed"] &= len(row["asr_transcripts"]) >= 2 and browser.notification_audio_chunks > 0
        row["passed"] &= browser.audio_chunks > 0 and row["session_tasks"] == 1
        return row
    finally:
        if task:
            barriers.mark(store, task["id"], "released")
            await manager.stop_remote(store.task(task["id"])["run_id"])
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", default=",".join(barriers.WINDOWS))
    parser.add_argument("--entries", default="http,voice")
    parser.add_argument("--qwen-live", action="store_true")
    parser.add_argument("--qwen-audio-live", action="store_true", help="Actual Qwen ASR/tool/audio validation using prerecorded PCM and local Tingting correction")
    parser.add_argument("--plan-one-live", action="store_true", help="Append six real-model plan-one samples with sanitized traces")
    parser.add_argument("--plan-cases", default="", help="Comma-separated plan-one sample names; default runs all samples")
    parser.add_argument("--skip-steer", action="store_true", help="Run only selected optional acceptance suites")
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/running-steer-validation.json")
    args = parser.parse_args()
    windows, entries = args.windows.split(","), args.entries.split(",")
    if set(windows) - set(barriers.WINDOWS) or set(entries) - {"http", "voice"}:
        parser.error("Unknown window or entry")
    if args.plan_cases:
        from experiments.running_steer.plan_one import SCENARIOS
        if set(args.plan_cases.split(",")) - {name for name, _, _ in SCENARIOS}:
            parser.error("Unknown plan-one sample")
        if not args.plan_one_live:
            parser.error("--plan-cases requires --plan-one-live")
    folder = ROOT / ".runtime" / ("running-steer-" + uuid.uuid4().hex[:10])
    folder.mkdir(mode=0o700)
    home = folder / "home"
    home.mkdir(mode=0o700)
    audio_clips = None
    if args.qwen_audio_live:
        from experiments.running_steer.audio import prepare
        audio_clips = prepare(ROOT, folder)
    source_home = ROOT / ".runtime/hermes-home"
    repo = ROOT / ".runtime/hermes-agent"
    gateway_sock = bound_socket()
    gateway_port = gateway_sock.getsockname()[1]
    app_sock = bound_socket()
    app_port = app_sock.getsockname()[1]
    key = secrets.token_urlsafe(32)
    config = json.loads((source_home / "config.yaml").read_text())
    config["gateway"]["api_server"].update(host="127.0.0.1", port=gateway_port, max_concurrent_runs=1)
    config["plugins"]["enabled"] = ["bailian-limits", "running-steer-validation"]
    db_path = folder / "tasks.sqlite3"
    config["mcp_servers"]["vehicle_preview"].update(
        args=["-m", "experiments.running_steer.mcp_server"], env={"PREVIEW_DB": str(db_path)}, timeout=60)
    private_write(home / "config.yaml", json.dumps(config, ensure_ascii=False))
    original = dotenv_values(source_home / ".env")
    private_write(home / ".env", "\n".join(f"{name}={json.dumps(value)}" for name, value in {
        **{k: v for k, v in original.items() if v is not None},
        "API_SERVER_KEY": key, "API_SERVER_ENABLED": "true", "API_SERVER_PORT": str(gateway_port),
        "API_SERVER_HOST": "127.0.0.1"}.items()) + "\n")
    shutil.copytree(ROOT / "hermes_plugins/bailian_limits", home / "plugins/bailian-limits",
                    ignore=shutil.ignore_patterns("__pycache__"))
    private_write(home / "plugins/running-steer-validation/__init__.py", "from experiments.running_steer.middleware import register\n")
    private_write(home / "plugins/running-steer-validation/plugin.yaml", "name: running-steer-validation\nversion: 0.1.0\ndescription: Production completion with timing telemetry\n")
    env = {**os.environ, **{k: v for k, v in original.items() if v is not None},
           "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(ROOT / ".runtime/hermes-tools"),
           "PREVIEW_DB": str(db_path), "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]),
           "RUNNING_STEER_AUDIT": str(folder / "middleware.jsonl"),
           "API_SERVER_KEY": key, "API_SERVER_ENABLED": "true", "API_SERVER_PORT": str(gateway_port), "API_SERVER_HOST": "127.0.0.1"}
    env["NO_PROXY"] = env.get("NO_PROXY", "") + ",localhost,127.0.0.1,::1,dashscope.aliyuncs.com"
    env["no_proxy"] = env["NO_PROXY"]
    # Reuse the installed interpreter/dependencies without installing or updating.
    os.environ["HERMES_HOME"] = str(source_home)
    os.environ["HERMES_RUNTIME_DIR"] = str(ROOT / ".runtime/hermes-tools")
    sys.path.insert(0, str(repo))
    from pm.environments import project_python, runtime_facts_path
    python = project_python(repo)
    facts = runtime_facts_path(repo)
    local_facts = home / facts.relative_to(source_home)
    private_write(local_facts, facts.read_text())
    (local_facts.parent / "environments").symlink_to(facts.parent / "environments", target_is_directory=True)
    settings = Settings(db_path=db_path, hermes_url=f"http://127.0.0.1:{gateway_port}", hermes_key=key, task_timeout=180)
    app = create_app(settings)
    barriers.setup(app.state.store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=app_port, log_level="error", access_log=False))
    app_job = asyncio.create_task(server.serve(sockets=[app_sock]))
    proc = None
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def report():
        args.output.write_text(json.dumps({"isolated": True, "production_policy_only": True,
            "app_port": app_port, "gateway_port": gateway_port,
            "model_configuration_copied_unchanged": True, "cases": rows}, ensure_ascii=False, indent=2) + "\n")
    try:
        await wait_for(lambda: server.started or app_job.done(), 15)
        if app_job.done():
            raise RuntimeError("Isolated application did not start")
        gateway_sock.close()
        fd = os.open(folder / "gateway.log", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "wb", buffering=0) as log:
            proc = subprocess.Popen([str(python), "-m", "gateway.run"], cwd=repo, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        async with httpx.AsyncClient(base_url=settings.hermes_url, headers={"Authorization": f"Bearer {key}"}, trust_env=False, timeout=3) as gateway:
            for _ in range(100):
                try:
                    response = await gateway.get("/v1/capabilities")
                    response.raise_for_status()
                    if response.json().get("features", {}).get("run_steer"):
                        break
                except httpx.HTTPError:
                    pass
                if proc.poll() is not None:
                    raise RuntimeError("Isolated gateway exited; private log retained")
                await asyncio.sleep(.3)
            else:
                raise RuntimeError("Isolated gateway did not become ready")
        print(json.dumps({"stage": "ready", "app_port": app_port, "gateway_port": gateway_port}), flush=True)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{app_port}", trust_env=False, timeout=25) as client:
            for window in ([] if args.skip_steer else windows):
                for entry in entries:
                    client.cookies.clear()
                    try:
                        row = await run_case(app, client, settings, entry, window)
                    except Exception as exc:
                        row = {"entry": entry, "window": window, "passed": False, "error_type": type(exc).__name__}
                    rows.append(row)
                    report()
                    print(json.dumps(row, ensure_ascii=False), flush=True)
            if args.plan_one_live:
                from experiments.running_steer.plan_one import SCENARIOS, run_case as run_plan_case
                for name, goal, profile in SCENARIOS:
                    if args.plan_cases and name not in args.plan_cases.split(","):
                        continue
                    row = await run_plan_case(app, client, name, goal, profile)
                    rows.append(row)
                    report()
                    print(json.dumps({k: v for k, v in row.items() if k not in {"trace", "receipts"}}, ensure_ascii=False), flush=True)
        if args.qwen_live:
            try:
                row = await run_qwen(app, settings)
            except Exception as exc:
                row = {"entry": "qwen_live_text", "passed": False, "error_type": type(exc).__name__}
            rows.append(row)
            report()
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if args.qwen_audio_live:
            try:
                row = await run_qwen(app, settings, audio_clips=audio_clips)
            except Exception as exc:
                row = {"entry": "qwen_live_prerecorded_audio", "passed": False, "error_type": type(exc).__name__, "physical_microphone_tested": False}
            rows.append(row)
            report()
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(app_job, 20)
        finally:
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.to_thread(proc.wait, 8)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await asyncio.to_thread(proc.wait)
            gateway_sock.close()
            app_sock.close()
        print("Isolated validation services stopped; existing services untouched.", flush=True)
    return 0 if rows and all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        # Upstream exception strings may include URLs or credentials.
        print(json.dumps({"passed": False, "error_type": type(error).__name__}), flush=True)
        raise SystemExit(2)
