import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalog import Catalog
from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.config import Settings
from app.hermes import HermesClient, TaskManager
from app.main import create_app
from app.preview_tools import PreviewTools
from app.realtime import NotificationGate
from app.store import Store


@pytest.fixture
def context(tmp_path):
    catalog = Catalog()
    store = Store(tmp_path / "test.db")
    sid = store.create_session()
    task, _ = store.create_task(sid, "把空气悬架升高一档", "key")
    return store, catalog, sid, task, PreviewTools(store, catalog)


def invoke(tool, task, name="air_suspension_set_height", **args):
    return tool.call(name, {"task_id": task["id"], "task_revision": task["revision"], **args})


def test_preview_only_and_idempotent(context):
    store, catalog, sid, task, tool = context
    for _ in range(2):
        result = invoke(tool, task, action="raise")
        assert result["executed"] is False
    calls = store.task(task["id"])["calls"]
    assert len(calls) == 1
    assert catalog.output(calls)["mode"] == "dry_run"
    duplicate, created = store.create_task(sid, task["goal"], "key")
    assert not created and duplicate["id"] == task["id"]
    with pytest.raises(ValueError):
        store.create_task(sid, "不同指令", "key")


@pytest.mark.parametrize("name,args", [
    ("air_suspension_set_height", {"action": "set"}),
    ("air_suspension_set_height", {"action": "raise", "height": "high"}),
    ("climate_set_temperature", {"zone": "all", "temperature_c": 50}),
    ("climate_set_temperature", {"zone": "all", "temperature_c": "23"}),
    ("seat_set_heating", {"seat": "driver", "level": 2.5}),
    ("media_set_volume", {"volume_percent": True}),
    ("window_set_position", {"window": "all", "open_percent": 50, "execute": True}),
    ("unregistered_control", {}),
])
def test_invalid_arguments_never_recorded(context, name, args):
    store, catalog, sid, task, tool = context
    result = invoke(tool, task, name, **args)
    assert result["error"] == "schema_validation_failed"
    assert store.task(task["id"])["calls"] == []


def test_clarification_resumes_original_and_rejects_old_revision(context):
    store, catalog, sid, task, tool = context
    result = invoke(tool, task, "request_user_input", question="要调高还是调低？")
    assert result["status"] == "awaiting_input"
    assert invoke(tool, task, action="raise")["error"] == "task_inactive"
    resumed = store.resume(task["id"], sid, "调高")
    assert resumed["id"] == task["id"] and resumed["revision"] == 1
    assert invoke(tool, task, action="raise")["error"] == "task_inactive"
    assert invoke(tool, resumed, action="raise")["status"] == "preview_generated"
    assert not store.events(sid, pending=True)  # Obsolete question is not read out later.


def test_cancel_rejects_late_calls_and_terminal_updates(context):
    store, catalog, sid, task, tool = context
    store.cancel(task["id"], sid)
    assert invoke(tool, task, action="raise")["error"] == "task_inactive"
    assert not store.finish(task["id"], "generated", "bad late result")
    assert store.task(task["id"])["status"] == "cancelled"


def test_session_isolation_and_restart(context):
    store, catalog, sid, task, tool = context
    other = store.create_session()
    with pytest.raises(KeyError):
        store.task(task["id"], other)
    with pytest.raises(KeyError):
        store.cancel(task["id"], other)
    store.interrupt_running()
    assert store.task(task["id"])["status"] == "interrupted"
    assert store.events(other) == []


def test_notification_gate_waits_for_user_model_and_playback():
    gate = NotificationGate()
    assert not gate.can_send(100)
    gate.ready = True
    for attr in ["user_speaking", "model_busy", "playback_busy"]:
        setattr(gate, attr, True)
        assert not gate.can_send(100)
        setattr(gate, attr, False)
    gate.audio_until = 101
    assert not gate.can_send(100)
    gate.audio_until = 0
    gate.last_activity = 99.9
    assert not gate.can_send(100)
    assert gate.can_send(101)


async def wait_job(manager, tid):
    job = manager.jobs.get(tid)
    if job:
        await asyncio.wait_for(job, 5)


async def test_hermes_runs_contract_requires_real_recorded_tool_call(context):
    store, _, sid, unused, _ = context
    catalog = CapabilityCatalog(store)
    preview = CapabilityTools(store, catalog)
    settings = Settings(db_path=store.path, hermes_key="test-not-secret")
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"features": {"run_submission": True}})
        if request.method == "POST" and request.url.path == "/v1/runs":
            body = json.loads(request.content)
            args = json.loads(body["input"])
            assert request.headers["Idempotency-Key"].startswith("preview-")
            assert args["task_revision"] == 0
            ctx = {"task_id": args["task_id"], "task_revision": 0}
            receipt = preview.call("call_tools", {**ctx, "catalog_revision": args["catalog_revision"], "calls": [{"call_id": "test-1", "tool_id": "control.suspension.set_height", "tool_version": "1.0.0", "arguments": {"action": "raise"}}]})["results"][0]
            preview.call("finish_task", {**ctx, "outcome": "prepared", "summary": "测试", "result_ids": [receipt["result_id"]]})
            return httpx.Response(202, json={"run_id": "run_test"})
        return httpx.Response(200, json={"status": "completed", "output": "已生成"})
    client = HermesClient(settings, httpx.AsyncClient(base_url="http://hermes", transport=httpx.MockTransport(handler)))
    manager = TaskManager(store, catalog, settings, client)
    task = manager.submit(sid, "升高空气悬架", "new")
    await wait_job(manager, task["id"])
    assert store.task(task["id"])["status"] == "generated"
    notices = store.events(sid, pending=True)
    assert notices[0]["kind"] == "task.generated"
    assert "模拟下发成功" in notices[0]["data"]["message"]
    await manager.close()


async def test_hermes_text_claim_is_not_success(context):
    store, catalog, sid, task, preview = context
    settings = Settings(db_path=store.path, hermes_key="test")
    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"features": {"run_submission": True}})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_test"})
        return httpx.Response(200, json={"status": "completed", "output": "我已经成功控制了车辆"})
    client = HermesClient(settings, httpx.AsyncClient(base_url="http://hermes", transport=httpx.MockTransport(handler)))
    manager = TaskManager(store, catalog, settings, client)
    await manager.run(task)
    assert store.task(task["id"])["status"] == "failed"
    assert not any(e["kind"] == "task.generated" for e in store.events(sid))
    await manager.close()


async def test_missing_credentials_does_not_fake_agent(context):
    store, catalog, sid, task, _ = context
    manager = TaskManager(store, catalog, Settings(db_path=store.path, hermes_key=""))
    await manager.run(task)
    task = store.task(task["id"])
    assert task["status"] == "failed" and task["calls"] == []
    await manager.close()


def test_http_example_catalog_and_session_isolation(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "web.db", hermes_key="", dashscope_key="", workspace=""))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/health").json()["vehicle_execution"] is False
        assert client.get("/api/tasks").status_code == 401
        assert client.post("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
        client.post("/api/session")
        catalog = client.get("/api/catalog").json()
        assert catalog["counts"] == {"control": 50, "manual": 8, "status": 10}
        result = client.post("/api/examples/control.suspension.set_height").json()
        assert result["status"] == "generated" and result["source"] == "fixture"
        assert result["output"]["executed"] is False
        assert result["output"]["calls"][0]["arguments"] == {"action": "raise"}
        assert "session_id" not in result and "run_id" not in result
        client.cookies.clear()
        client.post("/api/session")
        assert client.get(f"/api/tasks/{result['id']}").status_code == 404
        assert client.post(f"/api/tasks/{result['id']}/cancel").status_code == 404
        with client.websocket_connect("ws://localhost/ws/voice") as ws:
            assert ws.receive_json()["type"] == "error"


async def test_real_mcp_stdio_lists_and_validates_tools(tmp_path):
    """Actual MCP transport, no Hermes or LLM mocked into this protocol check."""
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from app.config import ROOT
    store = Store(tmp_path / "mcp.db")
    catalog = CapabilityCatalog(store)
    sid = store.create_session()
    task, _ = store.create_task(sid, "主驾加热二档", "mcp", catalog_revision=catalog.snapshot().revision, profile_id="demo-ready")
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT), env={**os.environ, "PREVIEW_DB": str(store.path)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"search_capabilities", "browse_capabilities", "describe_tools", "call_tools", "request_user_input", "wait_for_event", "finish_task"}
            discovered = await session.call_tool("search_capabilities", {"task_id":task["id"], "task_revision":0, "query":"当前空调温度", "kind":"status"})
            search_payload = json.loads(discovered.content[0].text)
            assert any(t["id"] == "status.climate.get_state" for t in search_payload["tools"])
            result = await session.call_tool("call_tools", {"task_id": task["id"], "task_revision": 0, "catalog_revision": task["catalog_revision"], "calls": [{"call_id": "seat-1", "tool_id": "control.seats.set_heating", "tool_version": "1.0.0", "arguments": {"seat": "driver", "level": 2}}]})
            assert not result.isError
            payload = json.loads(result.content[0].text)
            assert payload["results"][0]["status"] == "prepared" and not payload["results"][0]["executed"]
    assert store.task(task["id"])["calls"][0]["tool"] == "control.seats.set_heating"
    assert store.task(task["id"])["calls"][0]["arguments"] == {"seat": "driver", "level": 2}
