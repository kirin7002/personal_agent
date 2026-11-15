"""Sections 15/16 integration through production HTTP, manager, and S2S routes.

Remote Hermes I/O is deterministic; policy, persistence, routing, and expiry are
the real application. Wall time is injected only into app modules, leaving the
event loop and test timeout clock unchanged. Live checks have a separate script.
"""
import asyncio
from datetime import datetime
import json
import sys
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.hermes import HermesClient, TaskManager
from app.main import create_app
from app.realtime import VoiceBridge, TASK_TOOLS


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 16, 7, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


async def eventually(predicate, timeout=4):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


class Browser:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


@pytest.fixture
async def focus_rig(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    requests = []
    runs = 0

    async def upstream(request):
        nonlocal runs
        requests.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"features": {"run_submission": True, "run_steer": True}})
        if request.url.path == "/v1/runs":
            runs += 1
            return httpx.Response(202, json={"run_id": f"isolated-{runs}"})
        if request.url.path.endswith(("/stop", "/steer")):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"status": "running"})

    remote = httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(upstream))
    settings = Settings(db_path=tmp_path / "isolated.sqlite3", hermes_key="test-only", task_timeout=60)
    def factory(store, catalog, config):
        return TaskManager(store, catalog, config, client=HermesClient(config, remote))
    app = create_app(settings, manager_factory=factory)
    # Replace module references, not time.time itself: asyncio retains real time.
    for name, module in tuple(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "time", None) is time:
            monkeypatch.setattr(module, "time", clock)
    manager, store = app.state.manager, app.state.store
    manager.plans.clock = clock.time
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    (await http.post("/api/session")).raise_for_status()
    sid = http.cookies["preview_session"]
    bridge = VoiceBridge(Browser(), sid, settings, store, manager)
    sent = []
    async def send(message):
        sent.append(message)
    bridge.send = send
    manager.change_listeners.add(bridge.invalidate_task)
    rig = SimpleNamespace(app=app, manager=manager, store=store, settings=settings,
                          http=http, sid=sid, clock=clock, requests=requests, bridge=bridge, sent=sent)
    try:
        yield rig
    finally:
        manager.change_listeners.discard(bridge.invalidate_task)
        await manager.close()
        await http.aclose()


async def text(rig, value, event_id, **context):
    response = await rig.http.post("/api/conversation/input", json={"text": value, "event_id": event_id, **context})
    assert response.status_code in {200, 202}, response.text
    result = response.json()
    assert {"kind", "status", "message"} <= result.keys()
    return result


async def voice(rig, value, call_id, **context):
    entries = [entry["function"] for entry in TASK_TOOLS
               if "text" in entry["function"]["parameters"].get("properties", {})
               and any(word in entry["function"]["name"] for word in ("conversation", "input"))]
    assert len(entries) == 1, "Production S2S must expose one shared conversation input tool"
    event = {"name": entries[0]["name"], "call_id": call_id,
             "arguments": json.dumps({"text": value, **context})}
    await rig.bridge.handle_tool(event)
    await rig.bridge.handle_tool(event)
    outputs = [m["item"] for m in rig.sent if m.get("item", {}).get("type") == "function_call_output"
               and m["item"].get("call_id") == call_id]
    assert len(outputs) == 1
    result = json.loads(outputs[0]["output"])
    assert "error" not in result, result
    return result


async def ask(rig, goal, event_id):
    result = await text(rig, goal, event_id)
    task = result["task"]
    await eventually(lambda: bool(rig.store.task(task["id"])["run_id"]))
    rig.store.ask(task["id"], "要操作哪个分区？", task["revision"])
    await eventually(lambda: task["id"] not in rig.manager.jobs)
    return rig.store.task(task["id"])


def starts(rig):
    return sum(method == "POST" and path == "/v1/runs" for method, path, _ in rig.requests)


@pytest.mark.parametrize("entry", ["http", "voice"])
async def test_explicit_cancel_uses_shared_route_without_resume_or_new_task(focus_rig, entry):
    rig = focus_rig
    task = await ask(rig, "打开车窗", "create-window")
    before = starts(rig)
    if entry == "http":
        await text(rig, "算了", "cancel-window")
    else:
        await voice(rig, "算了", "cancel-window")
    assert rig.store.task(task["id"])["status"] == "cancelled"
    assert len(rig.store.tasks(rig.sid)) == 1
    assert starts(rig) == before


async def test_recent_user_focus_wins_over_older_background_progress(focus_rig):
    rig = focus_rig
    old = await ask(rig, "打开车窗", "old-window")
    current = await ask(rig, "把空调设为23度", "new-climate")
    rig.clock.advance(10)
    rig.store.event(old["id"], "agent.step", {"name": "browse_capabilities", "status": "success"})
    result = await text(rig, "全部", "answer-climate")
    assert result["task"]["id"] == current["id"]
    assert rig.store.task(current["id"])["revision"] == current["revision"] + 1
    assert rig.store.task(old["id"])["status"] == "awaiting_input"
    assert len(rig.store.tasks(rig.sid)) == 2


@pytest.mark.parametrize("value", ["不用全部，主驾就好", "别取消", "不是说取消"])
async def test_cancel_words_in_correction_or_negation_do_not_cancel(focus_rig, value):
    rig = focus_rig
    task = await ask(rig, "把空调设为23度", "climate")
    await text(rig, value, "not-cancel")
    assert rig.store.task(task["id"])["status"] != "cancelled"
    assert len(rig.store.tasks(rig.sid)) == 1


async def test_terminal_focus_does_not_fall_back_to_older_question(focus_rig):
    rig = focus_rig
    old = await ask(rig, "打开车窗", "old")
    recent = await ask(rig, "把空调设为23度", "recent")
    await text(rig, "算了", "cancel-recent")
    before = starts(rig)
    await text(rig, "全部", "unbound-answer")
    assert rig.store.task(recent["id"])["status"] == "cancelled"
    assert rig.store.task(old["id"])["status"] == "awaiting_input"
    assert len(rig.store.tasks(rig.sid)) == 2 and starts(rig) == before


@pytest.mark.parametrize("entry", ["http", "voice"])
async def test_five_minute_expiry_rejects_late_answer_without_new_model(focus_rig, entry):
    rig = focus_rig
    task = await ask(rig, "把空调设为23度", "expire-me")
    before = starts(rig)
    rig.clock.advance(301)
    sender = text if entry == "http" else voice
    result = await sender(rig, "全部", "late-answer", task_id=task["id"], task_revision=task["revision"])
    assert rig.store.task(task["id"])["status"] == "expired"
    assert result["status"] not in {"running", "accepted"}
    assert starts(rig) == before and len(rig.store.tasks(rig.sid)) == 1
    assert not any(e["kind"] == "task.question" for e in rig.store.events(rig.sid, pending=True))


async def test_answer_just_before_deadline_continues_same_task(focus_rig):
    rig = focus_rig
    task = await ask(rig, "把空调设为23度", "before-expiry")
    rig.clock.advance(299)
    result = await text(rig, "全部", "timely-answer", task_id=task["id"], task_revision=task["revision"])
    assert result["task"]["id"] == task["id"]
    assert rig.store.task(task["id"])["status"] == "running"
    assert rig.store.task(task["id"])["revision"] == 1


async def create_plan(rig, *, frequency="daily", weekdays=None, key="plan"):
    response = await rig.http.post("/api/plans", json={"goal": "查询空调温度", "request_key": key,
        "schedule": {"frequency": frequency, "time": "08:00", "weekdays": weekdays or []}})
    assert response.status_code in {200, 201, 202}, response.text
    result = response.json()
    return result.get("plan", result)


async def action(rig, plan, operation, **fields):
    response = await rig.http.post(f"/api/plans/{plan['id']}/action", json={
        "action": operation, "revision": plan["revision"], **fields})
    assert response.status_code in {200, 202}, response.text
    result = response.json()
    return result.get("plan", result)


@pytest.mark.parametrize("utterance,frequency,weekdays", [
    ("每天早上8点查询空调温度", "daily", []),
    ("每周一早上8点查询空调温度", "weekly", [0]),
])
async def test_natural_language_creates_daily_and_weekly_plan(focus_rig, utterance, frequency, weekdays):
    rig = focus_rig
    result = await text(rig, utterance, "recurring")
    plan = result["plan"]
    assert plan["schedule"]["frequency"] == frequency
    assert plan["schedule"]["time"] == "08:00"
    if weekdays:
        assert plan["schedule"]["weekdays"] == weekdays
    assert not rig.store.tasks(rig.sid) and starts(rig) == 0


async def test_missing_plan_time_requires_followup_without_assumed_time(focus_rig):
    rig = focus_rig
    result = await text(rig, "每天早上查询空调温度", "missing-time")
    assert not result.get("plan")
    assert result["status"] in {"awaiting_input", "needs_input", "clarification"}
    assert not rig.store.tasks(rig.sid) and starts(rig) == 0
    supplied = await text(rig, "早上8点", "supply-time")
    assert supplied["plan"]["schedule"]["time"] == "08:00"
    assert supplied["plan"]["schedule"]["frequency"] == "daily"


async def tick(rig, at=None):
    if at is not None:
        rig.clock.now = at
    await rig.manager.dispatch_plans()


async def test_background_plan_does_not_steal_foreground_question(focus_rig):
    rig = focus_rig
    plan = await create_plan(rig)
    foreground = await ask(rig, "把空调设为23度", "foreground")
    await tick(rig, plan["next_run_at"])
    run = rig.manager.plans.runs(rig.sid, plan["id"])[0]
    background = rig.store.task(run["task_id"])
    await eventually(lambda: bool(rig.store.task(background["id"])["run_id"]))
    rig.store.ask(background["id"], "后台查询需要补充信息", background["revision"])
    result = await text(rig, "全部", "foreground-answer")
    assert result["task"]["id"] == foreground["id"]
    assert rig.store.task(background["id"])["status"] == "awaiting_input"
    assert rig.manager.router.current(rig.sid)["id"] == foreground["id"]


async def test_pause_cancels_running_and_future_resume_only_future(focus_rig):
    rig = focus_rig
    plan = await create_plan(rig)
    await tick(rig, plan["next_run_at"])
    run = rig.manager.plans.runs(rig.sid, plan["id"])[0]
    tid = run["task_id"]
    await eventually(lambda: bool(rig.store.task(tid)["run_id"]))
    run_id = rig.store.task(tid)["run_id"]
    paused = await action(rig, plan, "pause")
    assert paused["status"] == "paused" and paused["next_run_at"] is None
    assert rig.store.task(tid)["status"] == "cancelled"
    assert any(path == f"/v1/runs/{run_id}/stop" for _, path, _ in rig.requests)
    assert not rig.store.events(rig.sid, pending=True)
    rig.clock.advance(3*86400)
    await tick(rig)
    assert len(rig.manager.plans.runs(rig.sid, plan["id"])) == 1
    resumed = await action(rig, paused, "resume")
    assert resumed["status"] == "pending" and resumed["next_run_at"] > rig.clock.time()
    await tick(rig)
    assert len(rig.store.tasks(rig.sid)) == 1
    assert rig.store.task(tid)["status"] == "cancelled"
    await tick(rig, resumed["next_run_at"])
    runs = rig.manager.plans.runs(rig.sid, plan["id"])
    assert len(runs) == 2 and runs[-1]["task_id"] != tid
    assert rig.store.task(tid)["status"] == "cancelled"


@pytest.mark.parametrize("active_status", ["running", "awaiting_input", "waiting"])
async def test_overlap_skips_without_queue_or_second_execution(focus_rig, active_status):
    rig = focus_rig
    plan = await create_plan(rig)
    await tick(rig, plan["next_run_at"])
    old = rig.manager.plans.runs(rig.sid, plan["id"])[0]
    tid = old["task_id"]
    await eventually(lambda: bool(rig.store.task(tid)["run_id"]))
    next_due = rig.manager.plans.get(rig.sid, plan["id"])["next_run_at"]
    rig.clock.now = next_due - .1
    if active_status == "awaiting_input":
        rig.store.ask(tid, "本次执行需要补充信息", 0)
    elif active_status == "waiting":
        rig.manager.loop_events.register_wait(rig.store.task(tid), "external_result", "pending-external",
                                             result_ref="test-external", continuation="等待测试回执")
    await tick(rig, next_due)
    runs = rig.manager.plans.runs(rig.sid, plan["id"])
    assert len(runs) == 2 and runs[-1]["status"] == "skipped"
    assert runs[-1]["reason"] == "previous_occurrence_active"
    assert runs[-1]["task_id"] is None and len(rig.store.tasks(rig.sid)) == 1
    await tick(rig)
    assert len(rig.manager.plans.runs(rig.sid, plan["id"])) == 2
    notices = [e for e in rig.manager.plans.notifications(rig.sid) if e["kind"] == "plan.skipped"]
    assert len(notices) == 1


async def test_missed_occurrence_notifies_once_without_catchup(focus_rig):
    rig = focus_rig
    plan = await create_plan(rig)
    await tick(rig, plan["next_run_at"] + 61)
    runs = rig.manager.plans.runs(rig.sid, plan["id"])
    assert len(runs) == 1 and runs[0]["status"] == "missed"
    assert runs[0]["reason"] == "late_beyond_grace" and runs[0]["task_id"] is None
    assert not rig.store.tasks(rig.sid) and starts(rig) == 0
    notices = [e for e in rig.manager.plans.notifications(rig.sid) if e["kind"] == "plan.missed"]
    assert len(notices) == 1
    await tick(rig)
    assert len(rig.manager.plans.runs(rig.sid, plan["id"])) == 1
    assert len([e for e in rig.manager.plans.notifications(rig.sid) if e["kind"] == "plan.missed"]) == 1
    assert rig.manager.plans.get(rig.sid, plan["id"])["next_run_at"] > rig.clock.time()


async def test_cancel_once_keeps_future_and_cancel_plan_stops_future(focus_rig):
    rig = focus_rig
    plan = await create_plan(rig)
    scheduled = plan["next_run_at"]
    once = await action(rig, plan, "cancel_once")
    assert once["status"] == "pending" and once["next_run_at"] > scheduled
    await tick(rig, scheduled)
    assert not rig.store.tasks(rig.sid)
    whole = await action(rig, once, "cancel_plan")
    assert whole["status"] == "cancelled" and whole["next_run_at"] is None
    await tick(rig, scheduled + 86400)
    assert not rig.store.tasks(rig.sid)


async def test_plan_action_revision_and_session_checks(focus_rig):
    rig = focus_rig
    plan = await create_plan(rig)
    paused = await action(rig, plan, "pause")
    stale = await rig.http.post(f"/api/plans/{plan['id']}/action", json={"action": "resume", "revision": plan["revision"]})
    assert stale.status_code == 409
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=rig.app), base_url="http://127.0.0.1")
    try:
        await other.post("/api/session")
        foreign = await other.post(f"/api/plans/{plan['id']}/action", json={"action": "resume", "revision": paused["revision"]})
        assert foreign.status_code == 404
    finally:
        await other.aclose()
    assert rig.manager.plans.get(rig.sid, plan["id"])["status"] == "paused"


@pytest.mark.parametrize("answer,expected", [("只取消这一次", "pending"), ("取消整个计划", "cancelled")])
async def test_ambiguous_plan_cancel_clarifies_scope_once(focus_rig, answer, expected):
    rig = focus_rig
    plan = (await text(rig, "每天早上8点查询空调温度", "create-for-cancel"))["plan"]
    result = await text(rig, "取消这个计划", "ambiguous-cancel")
    assert result["status"] == "awaiting_input"
    assert "一次" in result["message"] and "计划" in result["message"]
    assert rig.manager.plans.get(rig.sid, plan["id"])["revision"] == plan["revision"]
    resolved = await text(rig, answer, "cancel-scope")
    assert resolved["plan"]["id"] == plan["id"] and resolved["plan"]["status"] == expected
    assert resolved["kind"] != "clarify"
    assert rig.manager.dialog(rig.sid) is None


async def test_voice_plan_missing_time_uses_same_dialog_and_deduplicates(focus_rig):
    rig = focus_rig
    missing = await voice(rig, "每周一查询空调温度", "weekly-missing-time")
    assert missing["status"] == "awaiting_input" and not rig.manager.plans.list(rig.sid)
    supplied = await voice(rig, "早上8点", "weekly-time")
    plan = supplied["plan"]
    assert plan["schedule"]["frequency"] == "weekly" and plan["schedule"]["weekdays"] == [0]
    assert plan["schedule"]["time"] == "08:00"
    assert len(rig.manager.plans.list(rig.sid)) == 1
    assert not rig.store.tasks(rig.sid) and starts(rig) == 0


async def test_conversation_event_replay_is_idempotent_and_conflicts_are_rejected(focus_rig):
    rig = focus_rig
    first = await text(rig, "把所有分区空调设为23度", "one-event")
    duplicate = await text(rig, "把所有分区空调设为23度", "one-event")
    assert first["task"]["id"] == duplicate["task"]["id"]
    assert len(rig.store.tasks(rig.sid)) == 1
    conflict = await rig.http.post("/api/conversation/input", json={"text": "打开车窗", "event_id": "one-event"})
    assert conflict.status_code == 409 and len(rig.store.tasks(rig.sid)) == 1
    assert "session_id" not in first["task"] and "run_id" not in first["task"]


async def test_duplicate_question_or_chat_does_not_extend_expiry(focus_rig):
    rig = focus_rig
    task = await ask(rig, "把空调设为23度", "fixed-deadline")
    rig.clock.advance(299)
    rig.store.event(task["id"], "task.question", {"message": task["question"], "task_revision": task["revision"]}, True)
    await text(rig, "谢谢", "chat-near-deadline")
    rig.clock.advance(1)
    await rig.manager.expire_questions()
    assert rig.store.task(task["id"])["status"] == "expired"
    assert sum(e["kind"] == "task.expired" for e in rig.store.task_events(task["id"])) == 1


async def test_answer_racing_expiry_at_deadline_never_reopens_task(focus_rig):
    rig = focus_rig
    task = await ask(rig, "把空调设为23度", "deadline-race")
    before = starts(rig)
    rig.clock.advance(300)
    results = await asyncio.gather(
        rig.manager.expire_questions(),
        rig.manager.handle_input(rig.sid, "全部", "racing-answer", task["id"], task["revision"]),
        return_exceptions=True,
    )
    assert all(not isinstance(result, Exception) or isinstance(result, ValueError) for result in results)
    assert rig.store.task(task["id"])["status"] == "expired"
    assert starts(rig) == before and not rig.store.capability_results(task["id"])
    assert sum(e["kind"] == "task.expired" for e in rig.store.task_events(task["id"])) == 1


async def test_paused_plan_rejects_late_tool_and_answer(focus_rig):
    from app.capability_tools import CapabilityTools
    rig = focus_rig
    plan = await create_plan(rig)
    await tick(rig, plan["next_run_at"])
    task = rig.store.task(rig.manager.plans.runs(rig.sid, plan["id"])[0]["task_id"])
    await eventually(lambda: bool(rig.store.task(task["id"])["run_id"]))
    rig.store.ask(task["id"], "后台任务需要补充", task["revision"])
    await action(rig, plan, "pause")
    before = starts(rig)
    result = CapabilityTools(rig.store, rig.manager.catalog).call("call_tools", {
        "task_id": task["id"], "task_revision": task["revision"], "catalog_revision": task["catalog_revision"],
        "calls": [{"call_id": "late", "tool_id": "status.climate.get_state", "tool_version": "1.0.0", "arguments": {}}]})
    assert result.get("error")
    await text(rig, "全部", "late-paused-answer", task_id=task["id"], task_revision=task["revision"])
    assert rig.store.task(task["id"])["status"] == "cancelled"
    assert not rig.store.capability_results(task["id"]) and starts(rig) == before
