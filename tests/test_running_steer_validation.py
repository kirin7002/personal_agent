"""Deterministic production-path steer tests; no ProbeManager/ProbeTools guards.

Only the remote Hermes HTTP boundary and VoiceBridge network sinks are faked.
Run once the parent confirms the production revision implementation is ready.
"""
import asyncio
import base64
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.config import ROOT, Settings
from app.hermes import HermesClient, TaskManager
from app.main import create_app
from app.capability_tools import CapabilityTools
from experiments.running_steer import barriers
from experiments.running_steer.middleware import execute
from experiments.running_steer.voice import capture_bridge, generated_notification, revise_callback


TOOL = "control.climate.set_temperature"
TOOL_VERSION = json.loads((ROOT / "capabilities/tools/control/climate/set_temperature.json").read_text())["version"]
GOAL = "把所有分区空调设为23度"
CORRECTION = "改成21度"


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    settings = Settings(db_path=tmp_path / "tasks.sqlite3", hermes_key="isolated-test-key", task_timeout=10)
    requests = []

    async def remote(request):
        requests.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        path = request.url.path
        if path == "/v1/capabilities":
            return httpx.Response(200, json={"features": {"run_submission": True, "run_steer": True}})
        if path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "isolated-run"})
        if path.endswith("/steer"):
            return httpx.Response(200, json={"accepted": True, "run_id": "isolated-run"})
        if path.endswith("/stop"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"status": "running", "run_id": "isolated-run"})

    client = httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(remote))
    def factory(store, catalog, config):
        return TaskManager(store, catalog, config, client=HermesClient(config, client))
    app = create_app(settings, manager_factory=factory)
    store, manager = app.state.store, app.state.manager
    barriers.setup(store)
    monkeypatch.setenv("PREVIEW_DB", str(store.path))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    await http.post("/api/session")
    sid = http.cookies["preview_session"]
    result = SimpleNamespace(settings=settings, store=store, manager=manager, http=http,
                             sid=sid, requests=requests, service=CapabilityTools(store, manager.catalog))
    try:
        yield result
    finally:
        await manager.close()
        await http.aclose()


async def task_for(rig, window="before_control", goal=GOAL, mode="agent"):
    task = rig.manager.submit(rig.sid, goal, "task-" + window, completion_mode=mode)
    barriers.register(rig.store, task["id"], window)
    await until(lambda: rig.store.task(task["id"])["run_id"] is not None)
    return task


def call_args(task, temperature=23, revision=None, zone="all", call_id="old"):
    return {"task_id": task["id"], "task_revision": task["revision"] if revision is None else revision,
            "catalog_revision": task["catalog_revision"], "calls": [{"call_id": call_id,
            "tool_id": TOOL, "tool_version": TOOL_VERSION, "arguments": {"zone": zone, "temperature_c": temperature}}]}


def finish(rig, task, revision, ids):
    return rig.service.call("finish_task", {"task_id": task["id"], "task_revision": revision,
                           "summary": "按修改后的目标生成21度预览", "result_ids": ids})


def rejected(result):
    return bool(result.get("error")) or bool(result.get("results")) and all(
        r.get("error") or r.get("status") == "error" for r in result["results"])


async def revise_entry(rig, task, entry, bridge, sent, text=CORRECTION):
    if entry == "http":
        body = {"text": text, "task_revision": task["revision"], "change_id": "edit-1"}
        response = await rig.http.post(f"/api/tasks/{task['id']}/revise", json=body)
        assert response.status_code == 202, response.text
        duplicate = await rig.http.post(f"/api/tasks/{task['id']}/revise", json=body)
        assert duplicate.status_code == 202, duplicate.text
    else:
        await revise_callback(bridge, sent, task["id"], task["revision"], text)
    current = rig.store.task(task["id"])
    assert current["revision"] == task["revision"] + 1
    assert current["run_id"] == "isolated-run"
    change = rig.store.latest_change(task["id"])
    assert change and change["revision"] == current["revision"]
    assert "status" in change and "constraints" in change
    assert len([r for r in rig.requests if r[1].endswith("/steer")]) == 1
    return current


@pytest.mark.parametrize("entry", ["http", "voice"])
@pytest.mark.parametrize("window", barriers.WINDOWS)
async def test_three_windows_use_production_guards_and_one_native_run(rig, window, entry):
    task = await task_for(rig, window)
    bridge, sent = capture_bridge(rig.sid, rig.settings, rig.store, rig.manager)
    old_args = call_args(task)
    old_receipt = None
    provider_release = threading.Event()
    worker = None
    if window == "before_finish":
        old_receipt = rig.service.call("call_tools", old_args)["results"][0]
        held_args = {"task_id": task["id"], "task_revision": 0, "summary": "旧目标23度",
                     "result_ids": [old_receipt["result_id"]]}
        async def pending():
            await barriers.before_tool(rig.store, "finish_task", held_args)
            return rig.service.call("finish_task", held_args)
        worker = asyncio.create_task(pending())
    elif window == "before_control":
        async def pending():
            await barriers.before_tool(rig.store, "call_tools", old_args)
            return rig.service.call("call_tools", old_args)
        worker = asyncio.create_task(pending())
    else:
        request = {"model": "fixture", "messages": [{"role": "user", "content": json.dumps({
            "task_id": task["id"], "task_revision": 0, "catalog_revision": task["catalog_revision"],
            "user_request": task["goal"]})}]}
        def provider(_):
            if not provider_release.wait(5):
                raise TimeoutError("Fixture provider barrier timed out")
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="mcp__vehicle_preview__call_tools", input=deepcopy(old_args))])
        worker = asyncio.create_task(asyncio.to_thread(execute, request, provider, api_mode="anthropic_messages"))
    try:
        await barriers.wait_reached(rig.store, task["id"], timeout=3)
        current = await revise_entry(rig, task, entry, bridge, sent)
        assert rig.store.task(task["id"])["calls"] == []
        assert not any(e["kind"] == "task.generated" for e in rig.store.task_events(task["id"]))
        barriers.mark(rig.store, task["id"], "released")
        provider_release.set()
        held_result = await asyncio.wait_for(worker, 3)
        if window == "during_model":
            held_result = rig.service.call("call_tools", held_result.content[0].input)
        assert rejected(held_result)
        assert rejected(rig.service.call("call_tools", call_args(task, 23, current["revision"], call_id="stale-value")))
        assert rejected(rig.service.call("call_tools", call_args(task, 21, current["revision"], zone="driver", call_id="wrong-zone")))
        if old_receipt:
            assert rejected(finish(rig, task, current["revision"], [old_receipt["result_id"]]))
            assert any(r["result_id"] == old_receipt["result_id"] for r in rig.store.capability_results(task["id"]))
        good = rig.service.call("call_tools", call_args(task, 21, current["revision"], call_id="current"))["results"][0]
        assert good["status"] == "prepared"
        if old_receipt:
            assert rejected(finish(rig, task, current["revision"], [old_receipt["result_id"], good["result_id"]]))
        assert finish(rig, task, current["revision"], [good["result_id"]])["status"] == "generated"
        public = (await rig.http.get(f"/api/tasks/{task['id']}")).json()
        assert public["status"] == "generated"
        assert [c["arguments"] for c in public["calls"]] == [{"zone": "all", "temperature_c": 21}]
        assert "run_id" not in public and "session_id" not in public
        notice = await generated_notification(bridge, sent)
        assert notice["result"]["calls"][0]["arguments"] == {"zone": "all", "temperature_c": 21}
        assert notice["executed"] is False
        events = rig.store.task_events(task["id"])
        assert sum(e["kind"] == "task.generated" for e in events) == 1
        assert sum(e["kind"] == "task.dispatched" for e in events) == 1
        assert sum(method == "POST" and path == "/v1/runs" for method, path, _ in rig.requests) == 1
        # Let the production monitor observe the revised task and terminate itself.
        job = rig.manager.jobs.get(task["id"])
        if job:
            await asyncio.wait_for(asyncio.shield(job), 2)
        assert rig.store.task(task["id"])["status"] == "generated"
    finally:
        barriers.mark(rig.store, task["id"], "released")
        provider_release.set()
        if worker and not worker.done():
            await asyncio.wait_for(worker, 6)


@pytest.mark.parametrize("text", ["再舒服一点", "改成21度或22度", "把车窗打开"])
async def test_ambiguous_or_unsupported_revision_pauses_without_new_control(rig, text):
    task = await task_for(rig)
    current = await rig.manager.revise(rig.sid, task["id"], text, task["revision"], "ambiguous")
    assert current["status"] == "awaiting_input"
    assert current["question"]
    assert rejected(rig.service.call("call_tools", call_args(task)))
    assert rig.store.capability_results(task["id"]) == []
    assert not any(e["kind"] == "task.generated" for e in rig.store.task_events(task["id"]))


async def test_change_cas_session_isolation_and_terminal_rejection(rig):
    task = await task_for(rig)
    with pytest.raises((KeyError, ValueError)):
        await rig.manager.revise(rig.store.create_session(), task["id"], CORRECTION, 0, "foreign")
    with pytest.raises(ValueError):
        await rig.manager.revise(rig.sid, task["id"], CORRECTION, 99, "stale")
    assert rig.store.task(task["id"])["revision"] == 0
    current = await rig.manager.revise(rig.sid, task["id"], CORRECTION, 0, "edit")
    with pytest.raises(ValueError):
        await rig.manager.revise(rig.sid, task["id"], "改成20度", 0, "edit")
    good = rig.service.call("call_tools", call_args(task, 21, current["revision"], call_id="good"))["results"][0]
    assert finish(rig, task, current["revision"], [good["result_id"]])["status"] == "generated"
    with pytest.raises(ValueError):
        await rig.manager.revise(rig.sid, task["id"], "改成20度", current["revision"], "after-terminal")


async def test_production_plugin_closes_revised_completion_without_provider(rig):
    task = await task_for(rig)
    current = await rig.manager.revise(rig.sid, task["id"], CORRECTION, 0, "edit")
    good = rig.service.call("call_tools", call_args(task, 21, current["revision"], call_id="good"))["results"][0]
    assert finish(rig, task, current["revision"], [good["result_id"]])["status"] == "generated"
    # Include the original envelope only, as a native run's old history survives.
    request = {"model": "fixture", "messages": [{"role": "user", "content": json.dumps({
        "task_id": task["id"], "task_revision": 0, "catalog_revision": task["catalog_revision"],
        "user_request": task["goal"]})}]}
    def forbidden(_):
        pytest.fail("Production completion binding lost the revised native run")
    response = execute(request, forbidden, api_mode="anthropic_messages", session_id="vehicle-preview-" + task["id"])
    assert response.id == "local_vehicle_completion"
    assert response.usage.output_tokens == 0


@pytest.mark.parametrize("inflight", [False, True], ids=["queued_notice", "streaming_audio"])
async def test_revision_invalidates_old_voice_notice_and_late_audio(rig, inflight):
    task = await task_for(rig)
    bridge, sent = capture_bridge(rig.sid, rig.settings, rig.store, rig.manager)
    rig.store.event(task["id"], "task.question", {"message": "旧轮次的23度反馈", "task_revision": 0}, True)
    notice = rig.store.events(rig.sid, pending=True)[0]
    if inflight:
        bridge.gate.ready = True
        delivery = asyncio.create_task(bridge.deliver_notifications())
        try:
            await until(lambda: bridge.inflight == notice["id"] and notice["id"] in bridge.notice_items)
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        bridge.current_response = "old-response"
        bridge.response_notices["old-response"] = notice["id"]
        bridge.gate.model_busy = True
        bridge.gate.playback_busy = True
    current = await rig.manager.revise(rig.sid, task["id"], CORRECTION, 0, "change-with-notice")
    assert not rig.store.notification_current(rig.sid, notice["id"])
    assert notice["id"] not in {e["id"] for e in rig.store.events(rig.sid, pending=True)}
    invalidations = [m for m in bridge.browser.messages if m["type"] == "voice.invalidate"]
    if inflight:
        assert any(m['task_revision']==1 for m in invalidations)
    else:
        # A never-delivered notice needs queue invalidation only. Flushing the
        # global player here would discard another task's ongoing audio.
        assert not invalidations
    assert not bridge.gate.playback_busy and bridge.inflight is None
    if inflight:
        assert {"type": "response.cancel"} in sent
        assert any(m["type"] == "conversation.item.delete" for m in sent)
        assert bridge.stale_response("old-response")
        late = [
            {"type": "response.audio.delta", "response_id": "old-response", "delta": base64.b64encode(b"\0" * 480).decode()},
            {"type": "response.audio_transcript.delta", "response_id": "old-response", "delta": "旧23度结果"},
        ]
        class Upstream:
            def __aiter__(self):
                async def events():
                    for event in late:
                        yield json.dumps(event)
                return events()
        bridge.upstream = Upstream()
        count = len(bridge.browser.messages)
        await bridge.receive_model()
        assert not any(m["type"] in {"audio", "assistant.delta"} for m in bridge.browser.messages[count:])
    good = rig.service.call("call_tools", call_args(task, 21, current["revision"], call_id="new-notice"))["results"][0]
    assert finish(rig, task, current["revision"], [good["result_id"]])["status"] == "generated"
    event = await generated_notification(bridge, sent)
    assert event["result"]["calls"][0]["arguments"]["temperature_c"] == 21
    assert event["event_id"] != notice["id"]
