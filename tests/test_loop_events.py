"""SQLite concurrency and adapter-only tests: no model or virtual vehicle."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from app.loop_events import LoopEvents
from app.store import Store, TERMINAL


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "loop.db")
    sid = store.create_session()
    task, _ = store.create_task(sid, "Query after ten seconds", "original")
    return store, LoopEvents(store), sid, task


def race(*actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=5)
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [future.result(timeout=15) for future in futures]


def inbox_rows(store):
    with store.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM loop_inbox ORDER BY rowid")]


def test_inbox_replay_conflict_ownership_and_cas(env):
    store, loop, sid, task = env
    args = (sid, "one", "revise_goal", task["id"], 0, {"text": "change", "x": 1})
    record, created = loop.accept(*args)
    assert created and record["status"] == "pending"
    assert loop.mark_handled(sid, "one")
    assert not loop.mark_handled(sid, "one")
    store.revise_goal(task["id"], sid, "change", 0, "change-1", "new goal", {})
    replay, created = loop.accept(*args)
    assert not created and replay["status"] == "handled"
    assert loop.accept(sid, "one", "revise_goal", task["id"], 0,
                       {"x": 1, "text": "change"}) == (replay, False)
    with pytest.raises(ValueError, match="conflicts"):
        loop.accept(sid, "one", "cancel", task["id"], 0, args[-1])
    with pytest.raises(ValueError, match="revision"):
        loop.accept(sid, "two", "cancel", task["id"], 0, {})
    other = store.create_session()
    with pytest.raises(KeyError):
        loop.accept(other, "one", "cancel", task["id"], 1, {})
    with pytest.raises(KeyError):
        loop.mark_handled(other, "one")
    with pytest.raises(KeyError):
        loop.accept("missing", "one", "chat", None, None, {})
    assert len(inbox_rows(store)) == 1


def test_concurrent_inbox_accept_once(env):
    store, loop, sid, task = env
    action = lambda: loop.accept(sid, "same", "append_followup", task["id"], 0, {"text": "details"})
    results = race(action, action, action, action)
    assert sum(created for _, created in results) == 1
    assert len(inbox_rows(store)) == 1


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
def test_followup_only_after_terminal_and_once(env, terminal):
    store, loop, sid, task = env
    loop.accept(sid, "follow", "append_followup", task["id"], 0, {"text": "more"})
    loop.accept(sid, "thanks", "chat", None, None, {"text": "谢谢"})
    assert loop.drain_ready(sid) == []
    # Parent changes the goal; the followup still belongs to the original task.
    store.revise_goal(task["id"], sid, "change", 0, "change", "new goal", {})
    assert loop.drain_ready() == []
    assert store.finish(task["id"], terminal, "finished")
    other = store.create_session()
    assert loop.drain_ready(other) == []
    results = race(lambda: loop.drain_ready(sid), lambda: loop.drain_ready())
    claimed = [record for batch in results for record in batch]
    assert len(claimed) == 1
    assert claimed[0]["event_id"] == "follow"
    assert claimed[0]["revision"] == 0
    assert claimed[0]["payload"] == {"text": "more"}
    assert claimed[0]["status"] == "handled"
    assert loop.drain_ready(sid) == []
    assert len(store.tasks(sid)) == 1  # Parent, not Inbox, creates a linked task.
    assert store.task(task["id"])["revision"] == 1


def test_timer_uses_database_creation_time_and_atomic_outbox(env):
    store, loop, sid, task = env
    store.set_run(task["id"], "old-run")
    forged_snapshot = {**task, "created": task["created"] + 1000}
    wait = loop.register_wait(forged_snapshot, "timer", "timer-1", delay_seconds=10,
                              continuation="query temperature")
    assert wait["deadline"] == task["created"] + 10
    assert wait["status"] == "pending"
    waiting = store.task(task["id"])
    assert (waiting["status"], waiting["outcome"], waiting["revision"]) == ("waiting", "pending", 0)
    notices = store.events(sid, pending=True)
    assert len(notices) == 1 and notices[0]["kind"] == "task.waiting"
    assert store.notification_current(sid, notices[0]["id"])
    assert loop.consume_due(now=task["created"] + 9) == []
    resumed, = loop.consume_due(now=task["created"] + 10)
    assert (resumed["id"], resumed["status"], resumed["revision"], resumed["run_id"]) == (
        task["id"], "running", 1, "old-run")
    assert resumed["outcome"] is None
    assert resumed["history"][-1]["continuation"] == "query temperature"
    assert resumed["history"][-1]["kind"] == "timer_due"
    assert resumed["calls"] == []  # Timer is not a fabricated tool result.
    assert not store.events(sid, pending=True)
    assert not store.notification_current(sid, notices[0]["id"])
    assert loop.consume_due(now=task["created"] + 100) == []
    event, = inbox_rows(store)
    assert event["event_id"] == f"wait-{task['id']}-timer-1"
    assert (event["kind"], event["revision"], event["status"]) == ("timer_due", 0, "handled")
    assert json.loads(event["payload"]) == resumed["history"][-1]
    resumed_events = [e for e in store.events(sid) if e["kind"] == "task.resumed"]
    assert len(resumed_events) == 1
    assert resumed_events[0]["data"]["task_revision"] == 1


def test_timer_concurrent_consumers_and_registration(env):
    store, loop, sid, task = env
    register = lambda: loop.register_wait(task, "timer", "one", delay_seconds=0)
    assert race(register, register)[0] == loop.waits(task["id"], sid)[0]
    consume = lambda: loop.consume_due(now=task["created"])
    results = race(consume, consume, consume, consume)
    assert sum(len(result) for result in results) == 1
    assert store.task(task["id"])["revision"] == 1
    assert len(inbox_rows(store)) == 1
    assert len([e for e in store.events(sid) if e["kind"] == "task.waiting"]) == 1


def test_wait_replay_conflicts_and_budget(env):
    store, loop, sid, task = env
    original = task
    for index in range(3):
        wait = loop.register_wait(task, "timer", f"t{index}", delay_seconds=0)
        assert loop.register_wait(task, "timer", f"t{index}", delay_seconds=0) == wait
        task, = loop.consume_due(now=task["created"])
    assert loop.register_wait(original, "timer", "t0", delay_seconds=0)["status"] == "consumed"
    with pytest.raises(ValueError, match="conflicts"):
        loop.register_wait(original, "timer", "t0", delay_seconds=1)
    with pytest.raises(ValueError, match="budget"):
        loop.register_wait(task, "timer", "fourth", delay_seconds=0)
    assert len(loop.waits(task["id"], sid)) == 3
    assert store.task(task["id"])["status"] == "running"


def test_wait_requires_running_and_current_revision(env):
    store, loop, sid, task = env
    store.revise_goal(task["id"], sid, "change", 0, "change", "new goal", {})
    with pytest.raises(ValueError, match="running"):
        loop.register_wait(task, "timer", "stale", delay_seconds=0)
    current = store.task(task["id"])
    loop.register_wait(current, "timer", "active", delay_seconds=0)
    with pytest.raises(ValueError, match="running"):
        loop.register_wait(current, "external_result", "parallel", result_ref="job")
    assert len(loop.waits(task["id"], sid)) == 1


@pytest.mark.parametrize("status", ["done", "failed"])
def test_external_adapter_push_pending_then_terminal_once(env, status):
    store, loop, sid, task = env
    loop.register_wait(task, "external_result", "external", result_ref="job/42", continuation="review result")

    def adapter(result_status, payload):
        return loop.resume_external_result(task["id"], sid, "external", "job/42", result_status, payload)

    for _ in range(5):
        assert adapter("pending", {"progress": 0}) is None
    assert store.task(task["id"])["status"] == "waiting"
    assert loop.consume_due(now=task["created"] + 100000) == []
    assert inbox_rows(store) == []
    results = race(lambda: adapter(status, {"value": 42}), lambda: adapter(status, {"value": 42}))
    assert sum(result is not None for result in results) == 1
    resumed = next(result for result in results if result is not None)
    assert resumed["status"] == "running" and resumed["revision"] == 1
    assert resumed["history"][-1]["status"] == status
    assert resumed["history"][-1]["payload"] == {"value": 42}
    assert resumed["history"][-1]["result_ref"] == "job/42"
    assert adapter("pending", {}) is None  # Reordered push cannot regress a result.
    assert adapter(status, {"value": 42}) is None
    with pytest.raises(ValueError, match="conflicts"):
        adapter(status, {"value": 43})
    with pytest.raises(ValueError, match="conflicts"):
        adapter("failed" if status == "done" else "done", {"value": 42})
    event, = inbox_rows(store)
    assert (event["kind"], event["revision"], event["status"]) == ("external_result_ready", 0, "handled")
    assert loop.waits(task["id"], sid)[0]["result_status"] == status


def test_external_wrong_ref_task_session_status_never_resumes(env):
    store, loop, sid, task = env
    loop.register_wait(task, "external_result", "external", result_ref="job/42")
    other = store.create_session()
    other_task, _ = store.create_task(sid, "other", "other")
    loop.register_wait(other_task, "external_result", "external", result_ref="other-job")
    invalid = [
        (ValueError, task["id"], sid, "external", "wrong", "done"),
        (ValueError, other_task["id"], sid, "external", "job/42", "done"),
        (KeyError, task["id"], other, "external", "job/42", "done"),
        (KeyError, task["id"], sid, "wrong", "job/42", "done"),
        (ValueError, task["id"], sid, "external", "job/42", "success"),
    ]
    for error, *args in invalid:
        with pytest.raises(error):
            loop.resume_external_result(*args, {})
    assert all(t["status"] == "waiting" and t["revision"] == 0 for t in store.tasks(sid))
    assert inbox_rows(store) == []


@pytest.mark.parametrize("kind", ["timer", "external_result"])
def test_cancelled_or_stale_waits_cannot_revive(env, kind):
    store, loop, sid, task = env
    kwargs = {"delay_seconds": 0} if kind == "timer" else {"result_ref": "job"}
    loop.register_wait(task, kind, "wait", **kwargs)
    with pytest.raises(ValueError, match="Cancel or revise"):
        loop.cancel_waits(task["id"], sid)
    with pytest.raises(ValueError, match="revision"):
        loop.cancel_waits(task["id"], sid, expected_revision=1)
    with pytest.raises(KeyError):
        loop.cancel_waits(task["id"], store.create_session())
    store.cancel(task["id"], sid)
    # Callback/scheduler may arrive before the parent's explicit invalidation.
    if kind == "timer":
        assert loop.consume_due(now=task["created"]) == []
    else:
        assert loop.resume_external_result(task["id"], sid, "wait", "job", "done", {}) is None
    assert loop.cancel_waits(task["id"], sid) == 0
    assert loop.waits(task["id"], sid)[0]["status"] == "invalidated"
    assert store.task(task["id"])["status"] == "cancelled"
    assert store.task(task["id"])["revision"] == 0
    assert inbox_rows(store) == []
    assert [e["kind"] for e in store.events(sid, pending=True)] == ["task.cancelled"]


def test_revision_invalidation_preserves_new_wait_notice(env):
    store, loop, sid, task = env
    loop.register_wait(task, "timer", "old", delay_seconds=0)
    # Parent owns the goal-revision transition while waiting.
    with store.connect() as db:
        db.execute("UPDATE tasks SET status='running',revision=revision+1 WHERE id=?", (task["id"],))
    current = store.task(task["id"])
    loop.register_wait(current, "timer", "new", delay_seconds=100)
    assert loop.consume_due(now=task["created"]) == []
    waits = loop.waits(task["id"], sid)
    assert [w["status"] for w in waits] == ["invalidated", "pending"]
    notices = store.events(sid, pending=True)
    assert len(notices) == 1 and notices[0]["data"]["wait_id"] == "new"
    assert store.task(task["id"])["revision"] == 1


def test_cancel_races_due_consumption_never_revives_terminal(env):
    store, loop, sid, task = env
    loop.register_wait(task, "timer", "timer", delay_seconds=0)

    def cancel():
        store.cancel(task["id"], sid)
        return loop.cancel_waits(task["id"], sid)

    race(cancel, lambda: loop.consume_due(now=task["created"]))
    assert store.task(task["id"])["status"] == "cancelled"
    assert loop.consume_due(now=task["created"] + 100) == []
    assert loop.waits(task["id"], sid)[0]["status"] in {"invalidated", "consumed"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -1, True, "10", None, 10**400])
def test_bad_timer_values_leave_no_state(env, value):
    store, loop, sid, task = env
    with pytest.raises(ValueError):
        loop.register_wait(task, "timer", "bad", delay_seconds=value)
    assert loop.waits(task["id"], sid) == []
    assert store.task(task["id"])["status"] == "running"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_payloads_rejected_before_writes(env, value):
    store, loop, sid, task = env
    payload = {"nested": [{"value": value}]}
    with pytest.raises(ValueError):
        loop.accept(sid, "bad", "chat", None, None, payload)
    loop.register_wait(task, "external_result", "external", result_ref="job")
    with pytest.raises(ValueError):
        loop.resume_external_result(task["id"], sid, "external", "job", "done", payload)
    with pytest.raises(ValueError):
        loop.consume_due(now=value)
    assert store.task(task["id"])["revision"] == 0
    assert loop.waits(task["id"], sid)[0]["status"] == "pending"
    assert inbox_rows(store) == []


def test_outbox_failure_rolls_back_wait_and_resume(env, monkeypatch):
    store, loop, sid, task = env
    emit = store._event

    def fail(*args, **kwargs):
        raise RuntimeError("Outbox unavailable")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        loop.register_wait(task, "timer", "timer", delay_seconds=0)
    assert loop.waits(task["id"], sid) == []
    assert store.task(task["id"])["status"] == "running"
    monkeypatch.setattr(store, "_event", emit)
    loop.register_wait(task, "timer", "timer", delay_seconds=0)
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        loop.consume_due(now=task["created"])
    assert store.task(task["id"])["status"] == "waiting"
    assert store.task(task["id"])["history"] == []
    assert loop.waits(task["id"], sid)[0]["status"] == "pending"
    assert inbox_rows(store) == []
    assert len(store.events(sid, pending=True)) == 1


def test_resume_inbox_id_conflict_rolls_back(env):
    store, loop, sid, task = env
    loop.register_wait(task, "timer", "one", delay_seconds=0)
    loop.accept(sid, f"wait-{task['id']}-one", "chat", None, None, {})
    with pytest.raises(ValueError, match="conflicts"):
        loop.consume_due(now=task["created"])
    assert store.task(task["id"])["status"] == "waiting"
    assert loop.waits(task["id"], sid)[0]["status"] == "pending"


def test_existing_user_question_flow_remains_external(env):
    store, loop, sid, task = env
    store.ask(task["id"], "Which zone?")
    wait, = loop.waits(task["id"], sid)
    assert wait["kind"] == "user_answer"
    assert wait["wait_id"] == f"question-{task['id']}-0"
    assert wait["revision"] == 0 and wait["status"] == "pending"
    assert wait["expected_question"] == "Which zone?"
    assert loop.consume_due(now=task["created"] + 1000) == []
    assert loop.waits(task["id"], sid) == [wait]
    loop.accept(sid, "answer", "answer_question", task["id"], 0, {"text": "all"})
    with pytest.raises(ValueError, match="running"):
        loop.register_wait(task, "timer", "timer", delay_seconds=0)
    resumed = store.resume(task["id"], sid, "all", expected_revision=0)
    assert loop.mark_handled(sid, "answer")
    assert resumed["revision"] == 1 and resumed["history"][-1]["answer"] == "all"
    assert loop.waits(task["id"], sid) == []


def test_question_projection_tracks_revision_and_cancel_without_duplicate_state(env):
    store, loop, sid, task = env
    store.ask(task["id"], "Which zone?")
    store.revise_goal(task["id"], sid, "change", 0, "change", "new goal", {}, question="Which level?")
    wait, = loop.waits(task["id"], sid)
    assert wait["wait_id"] == f"question-{task['id']}-1"
    assert wait["revision"] == 1
    assert wait["expected_question"] == "Which level?"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM loop_waits").fetchone()[0] == 0
    with pytest.raises(KeyError):
        loop.waits(task["id"], store.create_session())
    store.cancel(task["id"], sid)
    assert loop.waits(task["id"], sid) == []
    assert loop.cancel_waits(task["id"], sid) == 0
