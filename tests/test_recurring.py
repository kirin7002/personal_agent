"""Real SQLite/fake-clock coverage. No model, catalogue service or control I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event

import pytest

from app.loop_events import LoopEvents
from app.recurring import (
    RecurringPlans, complete_schedule_request, local_timezone_name,
    next_occurrence, normalize_schedule, parse_schedule_request, parse_schedule_text,
)
from app.store import Store


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


class Clock:
    def __init__(self):
        self.now = epoch("2026-11-15T00:00:00+00:00")

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    store = Store(tmp_path / "recurring.sqlite3")
    clock = Clock()
    return store, RecurringPlans(store, clock=clock), store.create_session(), clock


def create(env, key="one", schedule=None, **kwargs):
    _, plans, sid, _ = env
    return plans.create(sid, key, "查询空调温度", schedule or {"frequency": "interval", "seconds": 60}, **kwargs)[0]


def tick(plans, now=None):
    return plans.due("mock-catalog-v1", "mock-default", "off", "agent", now=now)["tasks"]


def race(*actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=5)
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [future.result(timeout=15) for future in futures]


def test_create_contract_idempotency_ownership_and_edit(env):
    store, plans, sid, clock = env
    schedule = {"frequency": "daily", "time": "08:30", "weekdays": []}
    plan, created = plans.create(sid, "one", "查询空调温度", schedule)
    assert created
    assert {"id", "goal", "status", "revision", "next_run_at", "timezone", "schedule"} <= plan.keys()
    assert plan["status"] == "pending" and plan["revision"] == 0
    assert plan["timezone"] == plan["schedule"]["timezone"] == "Europe/Paris"
    assert plan["next_run_at"] == epoch("2026-11-15T08:30:00+01:00")
    assert plans.list(sid) == [plan]
    assert plans.create(sid, "one", "查询空调温度", schedule) == (plan, False)
    edited = plans.update(sid, plan["id"], 0, "edit", goal="查询座椅温度")["plan"]
    assert plans.create(sid, "one", "查询空调温度", schedule) == (edited, False)
    with pytest.raises(ValueError, match="conflicts"):
        plans.create(sid, "one", "新目标", schedule)
    with pytest.raises(ValueError, match="revision"):
        plans.update(sid, plan["id"], 0, "pause")
    other = store.create_session()
    for method in (plans.get, plans.runs):
        with pytest.raises(KeyError):
            method(other, plan["id"])
    with pytest.raises(KeyError):
        plans.update(other, plan["id"], 1, "cancel_plan")
    with pytest.raises(KeyError):
        plans.create("missing", "one", "goal", schedule)
    assert plans.list(other) == [] and store.tasks(sid) == []


def test_concurrent_create_and_due_deduplicate(env):
    store, plans, sid, clock = env
    args = (sid, "one", "goal", {"frequency": "interval", "seconds": 60})
    results = race(*(lambda: plans.create(*args) for _ in range(4)))
    assert sum(created for _, created in results) == 1
    plan = results[0][0]
    clock.now = plan["next_run_at"]
    results = race(*(lambda: tick(plans) for _ in range(4)))
    tasks = [task for batch in results for task in batch]
    assert len(tasks) == 1
    assert tasks[0]["catalog_revision"] == "mock-catalog-v1"
    assert tasks[0]["profile_id"] == "mock-default"
    assert tasks[0]["status"] == "running"
    assert len(store.tasks(sid)) == len(plans.runs(sid, plan["id"])) == 1
    assert len([e for e in store.events(sid) if e["kind"] == "task.created"]) == 1
    # Even replaying an old due pointer cannot bypass the occurrence unique key.
    with store.connect() as db:
        db.execute("UPDATE task_plans SET next_run_at=? WHERE id=?", (clock.now, plan["id"]))
    assert tick(plans) == []
    assert len(plans.runs(sid, plan["id"])) == 1


def test_due_profiles_are_snapshotted_without_live_catalog(env):
    store, plans, sid, clock = env
    explicit = create(env, "explicit", profile_id="pinned-mock")
    inherited = create(env, "inherited")
    store.select_profile(sid, "session-mock")
    clock.now = explicit["next_run_at"]
    tasks = tick(plans)
    by_plan = {p["id"]: store.task(plans.runs(sid, p["id"])[0]["task_id"]) for p in (explicit, inherited)}
    assert by_plan[explicit["id"]]["profile_id"] == "pinned-mock"
    assert by_plan[inherited["id"]]["profile_id"] == "session-mock"
    assert len(tasks) == 2


def test_due_rolls_back_claim_task_mapping_and_both_outboxes(env, monkeypatch):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    original = plans._event

    def fail(db, plan, kind, *args, **kwargs):
        if kind == "plan.triggered":
            raise RuntimeError("injected event write failure")
        return original(db, plan, kind, *args, **kwargs)

    monkeypatch.setattr(plans, "_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        tick(plans)
    assert store.tasks(sid) == store.events(sid) == plans.runs(sid, plan["id"]) == []
    assert plans.get(sid, plan["id"])["next_run_at"] == clock.now
    assert [event["kind"] for event in plans.events(sid)] == ["plan.created"]
    monkeypatch.setattr(plans, "_event", original)
    assert len(tick(plans)) == 1


@pytest.mark.parametrize("state", ["running", "awaiting_input", "waiting"])
def test_overlap_skips_all_active_states_and_does_not_block_other_plans(env, state):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    task = tick(plans)[0]
    if state == "awaiting_input":
        store.ask(task["id"], "哪个空调？")
    elif state == "waiting":
        LoopEvents(store).register_wait(task, "external_result", "external", result_ref="mock-ref")
    other = create(env, "other")
    clock.now += 60
    tasks = tick(plans)
    assert len(tasks) == 1
    assert plans.runs(sid, other["id"])[0]["task_id"] == tasks[0]["id"]
    skipped = plans.runs(sid, plan["id"])[-1]
    assert skipped["status"] == "skipped" and skipped["task_id"] is None
    assert skipped["reason"] == "previous_occurrence_active"
    assert store.task(task["id"])["status"] == state
    assert len([e for e in plans.notifications(sid) if e["kind"] == "plan.skipped"]) == 1
    assert tick(plans) == []


@pytest.mark.parametrize("late,expected", [(0, "created"), (60, "created"), (60.001, "missed")])
def test_grace_boundary_no_catchup(env, late, expected):
    store, plans, sid, clock = env
    plan = create(env, schedule={"frequency": "daily", "time": "08:00"})
    clock.now = plan["next_run_at"] + late
    tasks = tick(plans)
    assert bool(tasks) == (expected == "created")
    run = plans.runs(sid, plan["id"])[0]
    assert run["status"] == expected
    if expected == "missed":
        event = [e for e in plans.notifications(sid) if e["kind"] == "plan.missed"][0]
        assert event["data"]["reason"] == "late_beyond_grace"
        assert event["data"]["lateness_seconds"] == pytest.approx(late)
        assert "根因未确认" in event["data"]["message"]
        assert store.tasks(sid) == []
    assert tick(plans) == []


def test_elapsed_occurrences_recorded_without_backlog_runs(env):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now += 360.001
    tasks = tick(plans)
    runs = plans.runs(sid, plan["id"])
    assert len(tasks) == 1
    assert [r["status"] for r in runs] == ["missed"] * 5 + ["created"]
    assert len([e for e in plans.notifications(sid) if e["kind"] == "plan.missed"]) == 5
    assert plans.get(sid, plan["id"])["next_run_at"] > clock.now


@pytest.mark.parametrize("state", ["running", "awaiting_input", "waiting"])
@pytest.mark.parametrize("action", ["pause", "cancel_plan", "cancel_once"])
def test_stop_current_invalidates_callbacks_questions_and_notices(env, state, action):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    task = tick(plans)[0]
    store.set_run(task["id"], "remote-mock-run")
    loop = LoopEvents(store)
    if state == "awaiting_input":
        store.ask(task["id"], "哪个空调？")
    elif state == "waiting":
        loop.register_wait(task, "external_result", "external", result_ref="mock-ref")
    store.event(task["id"], "task.old_notice", {"message": "old notice"}, True)
    old_notices = store.events(sid, pending=True)
    result = plans.update(sid, plan["id"], 0, action)
    assert result["stopped_task_ids"] == [task["id"]]
    stopped = result["stopped_tasks"][0]
    assert stopped["status"] == "cancelled" and stopped["revision"] == task["revision"] + 1
    assert stopped["question"] is None and stopped["run_id"] == "remote-mock-run"
    assert all(not store.notification_current(sid, e["id"]) for e in old_notices)
    assert store.events(sid, pending=True) == []
    assert not store.finish(task["id"], "completed", "late result", expected_revision=0)
    with pytest.raises(ValueError):
        store.record_call(task["id"], "mock", {})
    with pytest.raises(ValueError):
        store.resume(task["id"], sid, "主驾", expected_revision=0)
    assert loop.consume_due(now=clock.now + 1000) == []
    if state == "waiting":
        assert loop.waits(task["id"], sid)[0]["status"] == "invalidated"
        assert loop.resume_external_result(task["id"], sid, "external", "mock-ref", "done", {}) is None
    if action in {"pause", "cancel_plan"}:
        assert result["plan"]["next_run_at"] is None
        assert tick(plans, clock.now + 600) == []
    else:
        assert result["plan"]["status"] == "pending"
        assert len(tick(plans, clock.now + 60)) == 1


def test_pause_preserves_completed_history_but_invalidates_success_notice(env):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    task = tick(plans)[0]
    store.finish(task["id"], "completed", "mock result")
    notice = store.events(sid, pending=True)[0]
    result = plans.update(sid, plan["id"], 0, "pause")
    assert result["stopped_task_ids"] == []
    assert store.task(task["id"])["status"] == "completed"
    assert store.task(task["id"])["summary"] == "mock result"
    assert not store.notification_current(sid, notice["id"])


def test_pause_stops_every_active_mapped_task_and_preserves_other_plans(env):
    store, plans, sid, clock = env
    plan = create(env)
    other = create(env, "other")
    clock.now = plan["next_run_at"]
    tick(plans)
    original = plans.runs(sid, plan["id"])[0]["task_id"]
    # Cover all active rows even if earlier callers left more than one mapping.
    extra_ids = []
    for offset, state in enumerate(("awaiting_input", "waiting"), 1):
        task, _ = store.create_task(sid, "legacy occurrence", f"extra-{offset}")
        extra_ids.append(task["id"])
        with store.connect() as db:
            db.execute("UPDATE tasks SET status=? WHERE id=?", (state, task["id"]))
            db.execute("""INSERT INTO task_plan_runs
                (plan_id,scheduled_at,task_id,plan_revision,status,created) VALUES(?,?,?,0,'created',?)""",
                       (plan["id"], clock.now + offset, task["id"], clock.now))
    result = plans.update(sid, plan["id"], 0, "pause")
    assert set(result["stopped_task_ids"]) == {original, *extra_ids}
    assert all(store.task(tid)["status"] == "cancelled" for tid in result["stopped_task_ids"])
    other_task = plans.runs(sid, other["id"])[0]["task_id"]
    assert store.task(other_task)["status"] == "running"


def test_failed_pause_rolls_back_task_wait_and_notifications(env, monkeypatch):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    task = tick(plans)[0]
    loop = LoopEvents(store)
    loop.register_wait(task, "timer", "timer", delay_seconds=60)
    notices = store.events(sid, pending=True)

    def fail(*args, **kwargs):
        raise RuntimeError("pause outbox failure")

    monkeypatch.setattr(plans, "_event", fail)
    with pytest.raises(RuntimeError):
        plans.update(sid, plan["id"], 0, "pause")
    assert plans.get(sid, plan["id"])["status"] == "pending"
    assert plans.get(sid, plan["id"])["revision"] == 0
    assert store.task(task["id"])["status"] == "waiting"
    assert store.task(task["id"])["revision"] == 0
    assert loop.waits(task["id"], sid)[0]["status"] == "pending"
    assert store.events(sid, pending=True) == notices
    assert plans.runs(sid, plan["id"])[0]["status"] == "created"


def test_edit_affects_only_future_run_snapshot(env):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    old = tick(plans)[0]
    revised = plans.update(sid, plan["id"], 0, "edit", goal="查询座椅温度", profile_id="new-mock",
                           schedule={"frequency": "interval", "seconds": 120})["plan"]
    assert store.task(old["id"])["goal"] == "查询空调温度"
    assert store.task(old["id"])["status"] == "running"
    store.finish(old["id"], "completed", "mock result")
    clock.now = revised["next_run_at"]
    new = tick(plans)[0]
    assert new["goal"] == "查询座椅温度" and new["profile_id"] == "new-mock"
    assert plans.runs(sid, plan["id"])[-1]["plan_revision"] == 1
    assert len(store.events(sid, pending=True)) == 1  # Each ordinary result uses task outbox.


@pytest.mark.parametrize("changes", [
    {"goal": "new goal"}, {"profile_id": "new-profile"},
    {"schedule": {"frequency": "interval", "seconds": 120}},
])
def test_any_edit_accounts_for_due_old_revision_and_reanchors_future(env, changes):
    store, plans, sid, _ = env
    plan, _ = plans.create(sid, "edit-race", "old goal", {"frequency": "interval", "seconds": 120}, now=1000)
    assert plan["next_run_at"] == 1120
    result = plans.update(sid, plan["id"], 0, "edit", now=1130, **changes)
    assert result["plan"]["next_run_at"] == 1250
    assert result["stopped_task_ids"] == []
    assert tick(plans, 1130) == []
    assert store.tasks(sid) == []
    skipped = plans.runs(sid, plan["id"])[0]
    assert skipped["status"] == "skipped" and skipped["reason"] == "plan_edited_before_dispatch"
    assert skipped["scheduled_at"] == 1120 and skipped["plan_revision"] == 0
    event = [e for e in plans.notifications(sid) if e["kind"] == "plan.skipped"][0]
    assert plans.notification_current(sid, event["id"])
    assert event["data"]["occurrence_plan_revision"] == 0
    new = tick(plans, 1250)[0]
    assert new["goal"] == changes.get("goal", "old goal")
    assert new["profile_id"] == changes.get("profile_id", "mock-default")
    assert plans.runs(sid, plan["id"])[-1]["plan_revision"] == 1


def test_edit_accounts_for_missed_old_occurrences_with_live_notices(env):
    _, plans, sid, _ = env
    plan, _ = plans.create(sid, "late-edit", "old goal", {"frequency": "interval", "seconds": 120}, now=1000)
    revised = plans.update(sid, plan["id"], 0, "edit", goal="new goal", now=1300)["plan"]
    assert revised["next_run_at"] == 1420
    assert [r["status"] for r in plans.runs(sid, plan["id"])] == ["missed", "skipped"]
    notices = [e for e in plans.notifications(sid) if e["kind"] in ("plan.missed", "plan.skipped")]
    assert len(notices) == 2
    assert all(plans.notification_current(sid, e["id"]) for e in notices)
    assert tick(plans, 1300) == []


def test_resume_only_future_without_resurrecting_runs(env):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    old = tick(plans)[0]
    paused = plans.update(sid, plan["id"], 0, "pause")["plan"]
    clock.now += 600
    resumed = plans.update(sid, plan["id"], paused["revision"], "resume")["plan"]
    assert resumed["next_run_at"] == clock.now + 60
    assert tick(plans) == []
    clock.now += 60
    new = tick(plans)[0]
    assert new["id"] != old["id"] and new["revision"] == 0
    assert store.task(old["id"])["status"] == "cancelled"
    assert [run["plan_revision"] for run in plans.runs(sid, plan["id"])] == [0, 2]
    assert len(plans.runs(sid, plan["id"])) == 2


def test_cancel_next_and_cancel_plan_terminal(env):
    _, plans, sid, clock = env
    plan = create(env)
    result = plans.update(sid, plan["id"], 0, "cancel_once")
    assert result["stopped_task_ids"] == []
    assert result["plan"]["next_run_at"] == plan["next_run_at"] + 60
    assert plans.runs(sid, plan["id"])[0]["status"] == "cancelled"
    assert tick(plans, plan["next_run_at"]) == []
    assert len(tick(plans, plan["next_run_at"] + 60)) == 1
    cancelled = plans.update(sid, plan["id"], 1, "cancel_plan")["plan"]
    for action in ("resume", "pause", "cancel_plan", "cancel_once", "edit"):
        with pytest.raises(ValueError, match="terminal"):
            plans.update(sid, plan["id"], cancelled["revision"], action)
    with pytest.raises(ValueError, match="Explicit action"):
        plans.update(sid, plan["id"], cancelled["revision"], "cancel")


def test_paused_cancel_once_has_no_target_and_edit_keeps_paused(env):
    _, plans, sid, clock = env
    plan = create(env)
    plans.update(sid, plan["id"], 0, "pause")
    with pytest.raises(ValueError, match="No active"):
        plans.update(sid, plan["id"], 1, "cancel_once")
    edited = plans.update(sid, plan["id"], 1, "edit", schedule={"frequency": "daily", "time": "20:00"})["plan"]
    assert edited["status"] == "paused" and edited["next_run_at"] is None
    assert tick(plans, clock.now + 86400) == []


def test_plan_notifications_independent_cursor_ack_and_invalidation(env):
    store, plans, sid, clock = env
    plan = create(env)
    original = plans.notifications(sid)[0]
    assert plans.notification_current(sid, original["id"])
    other = store.create_session()
    assert plans.events(other) == []
    assert not plans.notification_current(other, original["id"])
    plans.acknowledge(other, original["id"])
    assert plans.notification_current(sid, original["id"])
    plans.update(sid, plan["id"], 0, "pause")
    assert not plans.notification_current(sid, original["id"])
    latest = plans.notifications(sid, after=original["id"])[0]
    assert latest["kind"] == "plan.pause"
    assert plans.event_cursor(sid) == latest["id"]
    assert plans.notifications(sid) == [latest]
    plans.acknowledge(sid, latest["id"])
    plans.acknowledge(sid, latest["id"])
    assert not plans.notification_current(sid, latest["id"])
    assert plans.notifications(sid) == [] and store.events(sid) == []


@pytest.mark.parametrize("winner", ["due", "pause"])
def test_pause_due_race_both_serializations_are_safe(env, monkeypatch, winner):
    store, plans, sid, clock = env
    plan = create(env)
    clock.now = plan["next_run_at"]
    locked, release, contender_entered = Event(), Event(), Event()
    original = plans._event

    def hold(db, plan, kind, *args, **kwargs):
        if kind == ("plan.triggered" if winner == "due" else "plan.pause"):
            locked.set()
            assert release.wait(timeout=5)
        return original(db, plan, kind, *args, **kwargs)

    monkeypatch.setattr(plans, "_event", hold)
    actions = {"due": lambda: tick(plans), "pause": lambda: plans.update(sid, plan["id"], 0, "pause")}

    def contend():
        contender_entered.set()
        return actions["pause" if winner == "due" else "due"]()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(actions[winner])
        try:
            assert locked.wait(timeout=5)
            second = pool.submit(contend)
            assert contender_entered.wait(timeout=5)
            # Readers see no partially created task even while the due writer holds its lock.
            assert store.tasks(sid) == []
        finally:
            release.set()
        first_result, second_result = first.result(timeout=10), second.result(timeout=10)
    due_result, pause_result = (first_result, second_result) if winner == "due" else (second_result, first_result)
    assert plans.get(sid, plan["id"])["status"] == "paused"
    assert all(task["status"] == "cancelled" for task in store.tasks(sid))
    assert pause_result["stopped_task_ids"] == [task["id"] for task in due_result]
    for stale in due_result:
        current = store.task(stale["id"])
        assert current["revision"] != stale["revision"]  # Parent must reject stale launch snapshot.
    assert tick(plans, clock.now + 60) == []


def test_concurrent_control_compare_and_swap(env):
    _, plans, sid, _ = env
    plan = create(env)

    def change():
        try:
            return plans.update(sid, plan["id"], 0, "pause")
        except ValueError as exc:
            assert "revision" in str(exc)
            return None

    assert sum(result is not None for result in race(change, change)) == 1
    assert plans.get(sid, plan["id"])["revision"] == 1


@pytest.mark.parametrize("schedule", [
    {}, {"frequency": "monthly", "time": "08:00"},
    {"frequency": "daily"}, {"frequency": "daily", "time": "8:00"},
    {"frequency": "daily", "time": "24:00"}, {"frequency": "daily", "time": "08:60"},
    {"frequency": "daily", "time": "08:00", "weekdays": [0]},
    {"frequency": "weekly", "time": "08:00", "weekdays": []},
    {"frequency": "weekly", "time": "08:00", "weekdays": [True]},
    {"frequency": "weekly", "time": "08:00", "weekdays": [7]},
    {"frequency": "interval", "seconds": 59}, {"frequency": "interval", "seconds": 60.0},
    {"frequency": "interval", "seconds": True}, {"frequency": "interval", "seconds": float("inf")},
    {"frequency": "daily", "time": "08:00", "timezone": "Not/AZone"},
    {"frequency": "daily", "time": "08:00", "typo": 1},
])
def test_invalid_schedule_does_not_create_records(env, schedule):
    _, plans, sid, _ = env
    with pytest.raises(ValueError):
        plans.create(sid, "bad", "goal", schedule)
    assert plans.list(sid) == plans.events(sid) == []


def test_runtime_timezone_and_non_hour_offset(monkeypatch):
    monkeypatch.setenv("TZ", ":Asia/Kathmandu")
    assert local_timezone_name() == "Asia/Kathmandu"
    schedule = normalize_schedule({"frequency": "daily", "time": "00:15"})
    assert next_occurrence(schedule, epoch("2026-11-15T00:15:00+05:45")) == epoch("2026-11-16T00:15:00+05:45")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    assert normalize_schedule({"frequency": "daily", "time": "08:00"})["timezone"] == "America/Los_Angeles"


def test_weekly_monday_zero_multiple_days_and_exact_time():
    schedule = normalize_schedule({"frequency": "weekly", "time": "08:00", "weekdays": [2, 0, 2]}, timezone="Europe/Paris")
    assert schedule["weekdays"] == [0, 2]
    monday = next_occurrence(schedule, epoch("2026-11-15T09:00:00+01:00"))
    assert monday == epoch("2026-11-16T08:00:00+01:00")
    assert next_occurrence(schedule, monday) == epoch("2026-11-18T08:00:00+01:00")


def test_dst_gap_skipped_and_fold_first_only():
    schedule = normalize_schedule({"frequency": "daily", "time": "02:30"}, timezone="America/New_York")
    before = epoch("2026-03-07T02:30:00-05:00")
    assert next_occurrence(schedule, before) == epoch("2026-03-09T02:30:00-04:00")
    schedule["time"] = "01:30"
    first = next_occurrence(schedule, epoch("2026-10-31T03:00:00-04:00"))
    assert first == epoch("2026-11-01T01:30:00-04:00")
    next_day = epoch("2026-11-02T01:30:00-05:00")
    assert next_occurrence(schedule, first) == next_day
    assert next_occurrence(schedule, epoch("2026-11-01T01:15:00-05:00")) == next_day


def test_dst_daily_uses_wall_clock_not_86400_seconds():
    schedule = normalize_schedule({"frequency": "daily", "time": "08:00"}, timezone="America/New_York")
    before = epoch("2026-03-07T08:00:00-05:00")
    after = next_occurrence(schedule, before)
    assert after == epoch("2026-03-08T08:00:00-04:00")
    assert after - before == 23 * 3600
    # Lord Howe changes by half an hour, so fixed one-hour DST adjustments fail.
    schedule["timezone"] = "Australia/Lord_Howe"
    before = epoch("2026-10-03T08:00:00+10:30")
    assert next_occurrence(schedule, before) - before == 23.5 * 3600


def test_due_across_dst_repeated_hour_does_not_create_second_run(env):
    store, plans, sid, clock = env
    clock.now = epoch("2026-10-31T23:00:00-04:00")
    plan = create(env, schedule={"frequency": "daily", "time": "01:30", "timezone": "America/New_York"})
    clock.now = epoch("2026-11-01T01:30:00-04:00")
    first = tick(plans)[0]
    store.finish(first["id"], "completed", "first fold complete")
    clock.now = epoch("2026-11-01T01:30:00-05:00")
    assert tick(plans) == []
    assert plans.get(sid, plan["id"])["next_run_at"] == epoch("2026-11-02T01:30:00-05:00")
    assert len(plans.runs(sid, plan["id"])) == 1


@pytest.mark.parametrize("now", [True, -1, float("nan"), float("inf"), datetime(2026, 1, 1)])
def test_invalid_clock_is_rejected(env, now):
    _, plans, sid, _ = env
    with pytest.raises(ValueError):
        plans.create(sid, "clock", "goal", {"frequency": "interval", "seconds": 60}, now=now)


@pytest.mark.parametrize("text,time,days", [
    ("每天早上8点半查询空调温度", "08:30", []),
    ("每周一早上8点查询空调温度", "08:00", [0]),
    ("每周一、三、周五下午两点一刻查询空调温度", "14:15", [0, 2, 4]),
    ("每星期日23:10查询空调温度", "23:10", [6]),
    ("每天晚上十二点查询空调温度", "00:00", []),
    ("每天8点一键查询空调温度", "08:00", []),
])
def test_parse_explicit_schedules(text, time, days):
    parsed = parse_schedule_request(text, timezone="Europe/Paris")
    assert parsed["status"] == "ready" and parsed["missing"] == []
    assert parsed["goal"] in ("查询空调温度", "一键查询空调温度")
    assert parsed["schedule"]["time"] == time
    assert parsed["schedule"]["weekdays"] == days
    normalize_schedule(parsed["schedule"])


@pytest.mark.parametrize("text,reply,expected_time,expected_days", [
    ("每天早上查询空调温度", "8点", "08:00", []),
    ("每天下午查询空调温度", "两点半", "14:30", []),
    ("每周早上8点查询空调温度", "周一", "08:00", [0]),
    ("每天8点", "查询空调温度", "08:00", []),
    ("每周查询空调温度", "周一早上8点", "08:00", [0]),
])
def test_clarification_preserves_known_fields(text, reply, expected_time, expected_days):
    draft = parse_schedule_request(text, timezone="Europe/Paris")
    assert draft["status"] == "needs_info"
    before = repr(draft)
    parsed = complete_schedule_request(draft, reply)
    assert parsed["status"] == "ready"
    assert parsed["goal"] == "查询空调温度"
    assert parsed["schedule"]["time"] == expected_time
    assert parsed["schedule"]["weekdays"] == expected_days
    assert repr(draft) == before
    assert parse_schedule_request(reply, draft=draft) == parsed


@pytest.mark.parametrize("text,seconds", [("每分钟查询空调温度", 60), ("每隔60秒查询空调温度", 60),
                                        ("每两分钟查询空调温度", 120), ("每小时查询空调温度", 3600)])
def test_interval_text(text, seconds):
    parsed = parse_schedule_request(text, timezone="UTC")
    assert parsed["status"] == "ready" and parsed["goal"] == "查询空调温度"
    assert parsed["schedule"]["seconds"] == seconds
    normalize_schedule(parsed["schedule"])


@pytest.mark.parametrize("text,missing", [("每天早上查询空调温度", "time"), ("每周8点查询空调温度", "weekdays"),
                                         ("每月1日8点查询空调温度", "frequency"), ("每年查询空调温度", "frequency"),
                                         ("每隔30秒查询空调温度", "seconds"), ("每天25点查询空调温度", "time"),
                                         ("每天8:00:30查询空调温度", "time"), ("每天8点30秒查询空调温度", "time"),
                                         ("每天8点或者9点查询空调温度", "time"),
                                         ("每天早上8点和晚上9点查询空调温度", "time"),
                                         ("每天8点和9点查询空调温度", "time"), ("每周一到五8点查询空调温度", "weekdays")])
def test_missing_invalid_and_unsupported_are_never_one_off_goals(text, missing):
    parsed = parse_schedule_request(text, timezone="UTC")
    assert parsed["status"] == "needs_info" and missing in parsed["missing"]
    assert parsed["question"]
    assert parse_schedule_text(text, timezone="UTC")["question"]


def test_nonrecurring_and_incremental_clarifications():
    assert parse_schedule_request("查询空调温度")["status"] == "not_recurring"
    assert parse_schedule_text("查询空调温度") is None
    draft = parse_schedule_request("每周", timezone="UTC")
    draft = complete_schedule_request(draft, "周一")
    assert draft["missing"] == ["time", "goal"]
    draft = complete_schedule_request(draft, "8点")
    assert draft["missing"] == ["goal"]
    assert complete_schedule_request(draft, "查询空调温度")["status"] == "ready"
    draft = parse_schedule_request("每30秒查询空调温度", timezone="UTC")
    assert complete_schedule_request(draft, "60秒")["schedule"]["seconds"] == 60


@pytest.mark.parametrize("text,frequency,days", [
    ("我想每天早上8点查询空调温度", "daily", []),
    ("帮我在每天早上8点查询空调温度", "daily", []),
    ("以后每周一早上8点查询空调温度", "weekly", [0]),
    ("请你帮我设置一个每天早上8点查询空调温度", "daily", []),
    ("麻烦帮我以后每周一早上8点查询空调温度", "weekly", [0]),
])
def test_safe_common_request_prefixes(text, frequency, days):
    parsed = parse_schedule_request(text, timezone="UTC")
    assert parsed["status"] == "ready"
    assert parsed["schedule"]["frequency"] == frequency
    assert parsed["schedule"]["weekdays"] == days
    assert parsed["schedule"]["time"] == "08:00"
    assert parsed["goal"] == "查询空调温度"


@pytest.mark.parametrize("text", [
    "我想让你每天早上8点查询空调温度", "从明天起每天早上8点查询空调温度",
    "能不能每周一早上8点查询空调温度", "我想每月1日查询空调温度",
    "请你到时每隔两分钟查询空调温度", "在下个月开始每个星期查询空调温度",
])
def test_unparsed_recurring_hint_never_falls_through_to_immediate_task(text):
    parsed = parse_schedule_request(text, timezone="UTC")
    assert parsed["status"] == "needs_info" and parsed["question"]
    assert parse_schedule_text(text, timezone="UTC") is not None


@pytest.mark.parametrize("text", [
    "暂停每天8点的计划", "取消每天8点的计划", "帮我恢复每天8点的计划",
    "帮我把每天8点的计划暂停", "以后每周一的计划取消", "停止每分钟查询空调温度",
    "删除每周一查询空调温度的计划",
])
def test_plan_control_commands_are_left_to_parent_router(text):
    assert parse_schedule_request(text, timezone="UTC")["status"] == "not_recurring"
    assert parse_schedule_text(text, timezone="UTC") is None


def test_recurring_cancellation_goal_is_not_plan_control():
    parsed = parse_schedule_request("每天8点取消预约", timezone="UTC")
    assert parsed["status"] == "ready" and parsed["goal"] == "取消预约"
    assert parse_schedule_request("查询每个空调的温度")["status"] == "not_recurring"


@pytest.mark.parametrize("text", [
    "每天早上8点和晚上9点查询空调温度", "每天8点或者9点查询空调温度",
    "每天8点、下午2点以及晚上9点查询空调温度",
])
def test_multiple_clock_times_need_clarification_and_keep_goal(text):
    draft = parse_schedule_request(text, timezone="UTC")
    assert draft["status"] == "needs_info" and draft["missing"] == ["time"]
    assert "time" not in draft["schedule"]
    assert draft["goal"] == "查询空调温度"
    parsed = complete_schedule_request(draft, "早上8点")
    assert parsed["status"] == "ready" and parsed["schedule"]["time"] == "08:00"
    assert parsed["goal"] == "查询空调温度"
