"""Canonical history, reconnect and revision tests; no models or transport."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from app.device_sharing import DeviceSharing
from app.recurring import RecurringPlans
from app.shared_messages import MessageLog
from app.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "messages.db")
    return store, MessageLog(store), store.create_session()


def race(*actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=5)
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [f.result(timeout=15) for f in futures]


def test_append_replay_final_voice_update_and_reconnect(env):
    _, log, sid = env
    args = (sid, "utterance-1", "user", "查空调", "voice")
    message = log.append(*args, final=False)
    assert type(message["id"]) is int and message["version"] == 1
    first = log.snapshot(sid)
    updated = log.update_by_key(sid, "utterance-1", 1, text="查询空调温度", final=True)
    assert updated["id"] == message["id"] and updated["version"] == 2
    assert log.append(*args, final=False) == updated
    assert log.by_key(sid, "utterance-1") == updated
    assert log.by_key(sid, "unknown") is None
    with pytest.raises(ValueError, match="conflicts"):
        log.append(sid, "utterance-1", "user", "different", "voice", final=False)
    with pytest.raises(ValueError, match="version"):
        log.update_by_key(sid, "utterance-1", 1, text="stale")
    assert log.update_by_key(sid, "utterance-1", 2, text=updated["text"]) == updated
    deltas = log.events(sid, first["cursor"])
    assert len(deltas["events"]) == 1 and deltas["events"][0]["message"] == updated
    assert log.events(sid, deltas["cursor"])["events"] == []
    assert log.snapshot(sid)["messages"] == [updated]


def test_pagination_scope_and_global_event_order(env):
    store, log, sid = env
    other = store.create_session()
    own = []
    for i in range(5):
        own.append(log.append(sid, f"m{i}", "user", str(i), "text"))
        log.append(other, f"m{i}", "user", "private", "text")
    first = log.snapshot(sid, limit=2)
    assert first["messages"] == own[-2:] and first["has_more"]
    second = log.snapshot(sid, before=first["next_before"], limit=2)
    assert second["messages"] == own[1:3]
    third = log.snapshot(sid, before=second["next_before"], limit=2)
    assert third["messages"] == own[:1] and not third["has_more"]
    batch, cursor = [], 0
    while True:
        page = log.events(sid, cursor, limit=2)
        batch.extend(page["events"])
        assert page["cursor"] > cursor
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert [event["message"] for event in batch] == own
    assert [event["seq"] for event in batch] == [1, 3, 5, 7, 9]
    assert cursor == 10
    with pytest.raises(KeyError):
        log.update(other, own[0]["id"], text="attack")
    assert log.by_key(other, "not-there") is None


def test_message_ids_and_delta_sequence_are_distinct(env):
    _, log, sid = env
    one = log.append(sid, "one", "assistant", "partial", "voice", final=False)
    process = log.emit_event(sid, "voice.process", {"device_id": "display-only", "step": "thinking"})
    assert process["kind"] == "voice.process" and "message" not in process
    log.update(sid, one["id"], text="final", final=True)
    two = log.append(sid, "two", "user", "next", "text")
    assert two["id"] == one["id"] + 1
    events = log.events(sid)["events"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[2]["message"]["id"] == one["id"]
    assert len(log.snapshot(sid)["messages"]) == 2
    with pytest.raises(ValueError):
        log.emit_event(sid, "message.upsert", {"text": "fake"})


def test_concurrent_append_and_version_cas(env):
    _, log, sid = env
    args = (sid, "same", "user", "hello", "text")
    results = race(*(lambda: log.append(*args) for _ in range(4)))
    assert len({m["id"] for m in results}) == 1
    assert len(log.events(sid)["events"]) == 1

    def change(text):
        try:
            return log.update_by_key(sid, "same", 1, text=text)
        except ValueError as exc:
            assert "version" in str(exc)
            return None

    assert sum(m is not None for m in race(lambda: change("one"), lambda: change("two"))) == 1
    assert len(log.events(sid)["events"]) == 2


def test_linking_ownership_and_rebound_device(env):
    store, log, sid = env
    other = store.create_session()
    task, _ = store.create_task(sid, "goal", "task")
    foreign, _ = store.create_task(other, "private", "task")
    message = log.append(sid, "turn", "user", "query", "voice")
    linked = log.update_by_key(sid, "turn", task_id=task["id"])
    assert linked["task_revision"] == 0 and linked["task_status"] == "running"
    assert linked["version"] == 2 and linked["id"] == message["id"]
    with pytest.raises(KeyError):
        log.update(sid, message["id"], task_id=foreign["id"])
    sharing = DeviceSharing(store)
    device = sharing.issue(sid, "desktop", "desktop", trusted=True)
    log.append(sid, "device-turn", "user", "hello", "text", device_id=device["device"]["id"])
    owner = sharing.issue(other, "other desktop", "desktop", trusted=True)
    sharing.redeem(device["device"]["id"], sharing.create_code(owner["device"]["id"])["code"])
    with pytest.raises(KeyError):
        log.append(sid, "device-turn", "user", "hello", "text", device_id=device["device"]["id"])
    assert log.snapshot(other)["messages"] == []  # No history merge.


def test_task_sync_is_canonical_dedup_and_readers_never_ack(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.ask(task["id"], "哪个空调？")
    event = store.events(sid, pending=True)[0]
    first = log.sync_task_event(sid, event)
    assert first["key"] == f"event:{event['id']}" and first["text"] == "哪个空调？"
    assert first["raw_source"]["data"] == event["data"]
    assert first["task_revision"] == 0 and first["task_status"] == "awaiting_input"
    # Final voice presentation modifies this same message, not a second bubble.
    voiced = log.update_by_key(sid, first["key"], final=True)
    log.sync_events(sid)
    assert log.snapshot(sid)["messages"] == [voiced]
    assert len(log.events(sid)["events"]) == 1
    assert store.events(sid, pending=True) == [event]
    assert store.notification_current(sid, event["id"])
    assert log.sync_events(sid)["messages"] == []
    other = store.create_session()
    with pytest.raises(KeyError):
        log.sync_task_event(other, event)


def test_revise_supersedes_old_question_and_preserves_user_history(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    user = log.append(sid, "user", "user", "query", "text", task_id=task["id"])
    store.ask(task["id"], "哪个空调？")
    log.sync_events(sid)
    question = log.snapshot(sid)["messages"][-1]
    cursor = log.snapshot(sid)["cursor"]
    store.revise_goal(task["id"], sid, "座椅", 0, "edit", "查询座椅", {}, question="哪个座椅？")
    log.sync_events(sid)
    messages = log.snapshot(sid)["messages"]
    assert messages[0] == user and not messages[0]["superseded"]
    assert messages[1]["id"] == question["id"] and messages[1]["superseded"]
    assert messages[1]["version"] == question["version"] + 1
    assert messages[2]["text"] == "哪个座椅？" and not messages[2]["superseded"]
    assert len(log.events(sid, cursor)["events"]) == 2


def test_final_voice_rephrasing_keeps_source_question_current(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.ask(task["id"], "哪个空调？")
    source_event = store.events(sid, pending=True)[0]
    question = log.sync_task_event(sid, source_event)
    spoken = log.update_by_key(sid, question["key"], text="请问您指的是哪个空调？", final=True)
    assert not spoken["superseded"] and spoken["version"] == 2
    log.sync_events(sid)
    assert log.sync_task_event(sid, source_event) == spoken
    assert log.snapshot(sid)["messages"] == [spoken]
    assert store.notification_current(sid, source_event["id"])
    store.cancel(task["id"], sid)
    log.sync_events(sid)
    assert log.by_key(sid, question["key"])["superseded"]


def test_snapshot_and_cursor_share_snapshot_during_concurrent_append(env, monkeypatch):
    _, log, sid = env
    anchored, release = Event(), Event()
    original = log._cursor

    def hold(db):
        cursor = original(db)  # Anchor the SQLite read snapshot before writer commits.
        anchored.set()
        assert release.wait(timeout=5)
        return cursor

    monkeypatch.setattr(log, "_cursor", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        snapshot = pool.submit(log.snapshot, sid)
        try:
            assert anchored.wait(timeout=5)
            new = log.append(sid, "new", "user", "arrived during snapshot", "text")
        finally:
            release.set()
        initial = snapshot.result(timeout=10)
    assert initial["messages"] == [] and initial["cursor"] == 0
    monkeypatch.setattr(log, "_cursor", original)
    changes = log.events(sid, initial["cursor"])
    assert [e["message"] for e in changes["events"]] == [new]


def test_message_readers_do_not_invalidate_spoken_or_pending_notifications(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.finish(task["id"], "completed", "recorded result")
    event = store.events(sid, pending=True)[0]
    store.acknowledge(sid, event["id"])
    before = store.events(sid)
    log.sync_events(sid)
    assert log.snapshot(sid)["messages"][0]["text"] == "recorded result"
    log.events(sid)
    assert store.events(sid) == before
    assert store.events(sid, pending=True) == []


def test_stale_final_callback_cannot_revalidate_superseded_message(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    message = log.append(sid, "voice", "assistant", "working", "voice", task_id=task["id"], final=False)
    store.revise_goal(task["id"], sid, "new", 0, "edit", "new goal", {})
    late = log.update(sid, message["id"], text="old completed result", final=True, superseded=False)
    assert late["superseded"] and late["task_revision"] == 0
    assert late["superseded_reason"] == "task_revision_changed"


def test_plan_linking_scoped_and_preserves_original_snapshot(env):
    store, log, sid = env
    plans = RecurringPlans(store, timezone="UTC")
    plan, _ = plans.create(sid, "one", "goal", {"frequency": "interval", "seconds": 60}, now=1000)
    other = store.create_session()
    foreign, _ = plans.create(other, "one", "private", {"frequency": "interval", "seconds": 60}, now=1000)
    message = log.append(sid, "one", "user", "daily request", "text")
    linked = log.update_by_key(sid, "one", plan_id=plan["id"])
    assert linked["plan_revision"] == 0 and linked["id"] == message["id"]
    with pytest.raises(KeyError):
        log.update_by_key(sid, "one", plan_id=foreign["id"])
    assert log.by_key(sid, "one") == linked


def test_untrusted_and_revoked_device_cannot_append(env):
    store, log, sid = env
    sharing = DeviceSharing(store)
    remote = sharing.issue(sid, "remote", "phone")
    with pytest.raises(KeyError):
        log.append(sid, "remote", "user", "not paired", "text", device_id=remote["device"]["id"])
    trusted = sharing.issue(sid, "local", "desktop", trusted=True)
    sharing.revoke(sid, trusted["device"]["id"])
    with pytest.raises(KeyError):
        log.append(sid, "trusted", "user", "revoked", "text", device_id=trusted["device"]["id"])
    assert log.snapshot(sid)["messages"] == []


@pytest.mark.parametrize("status", ["cancelled", "expired", "completed", "failed"])
def test_late_sync_marks_old_question_superseded_and_keeps_terminal_text(env, status):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.ask(task["id"], "question")
    # Simulate the same transaction shape used by cancellation/expiry handlers.
    with store.connect() as db:
        db.execute("UPDATE tasks SET status=?,question=NULL,revision=revision+1 WHERE id=?", (status, task["id"]))
        current = store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
        store._event(db, current, "task." + status, {"message": "actual " + status, "status": status}, True)
    log.sync_events(sid)
    messages = log.snapshot(sid)["messages"]
    assert messages[0]["superseded"]
    assert messages[1]["text"] == "actual " + status and not messages[1]["superseded"]
    assert messages[1]["task_status"] == status
    assert all(not event["spoken"] for event in store.events(sid))


def test_cancel_without_revision_increment_invalidates_question(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.ask(task["id"], "question")
    log.sync_events(sid)
    store.cancel(task["id"], sid)
    log.sync_events(sid)
    messages = log.snapshot(sid)["messages"]
    assert messages[0]["superseded"] and messages[1]["kind"] == "task.cancelled"
    assert not messages[1]["superseded"]


def test_plan_misses_and_skips_import_once_without_spoken_mutation(env):
    store, log, sid = env
    plans = RecurringPlans(store, timezone="UTC")
    plan, _ = plans.create(sid, "plan", "query", {"frequency": "interval", "seconds": 120}, now=1000)
    plans.due("mock", "mock", "off", "agent", now=1181)  # 61 seconds late -> missed.
    plans.due("mock", "mock", "off", "agent", now=1240)  # Start current execution.
    plans.due("mock", "mock", "off", "agent", now=1360)  # Previous still active -> skip.
    before = plans.notifications(sid)
    results = race(lambda: log.sync_events(sid), lambda: log.sync_events(sid))
    assert sum(len(r["messages"]) for r in results) == 2
    messages = log.snapshot(sid)["messages"]
    assert [m["kind"] for m in messages] == ["plan.missed", "plan.skipped"]
    assert all(m["plan_id"] == plan["id"] and m["raw_source"]["stream"] == "plan" for m in messages)
    assert plans.notifications(sid) == before
    assert log.sync_events(sid)["messages"] == []


def test_sync_and_append_roll_back_if_delta_write_fails(env, monkeypatch):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.finish(task["id"], "completed", "actual result")
    original = log._upsert_event

    def fail(*args):
        raise RuntimeError("delta insert failed")

    monkeypatch.setattr(log, "_upsert_event", fail)
    with pytest.raises(RuntimeError):
        log.sync_events(sid)
    with pytest.raises(RuntimeError):
        log.append(sid, "one", "user", "hello", "text")
    assert log.snapshot(sid)["messages"] == [] and log.events(sid)["events"] == []
    monkeypatch.setattr(log, "_upsert_event", original)
    assert len(log.sync_events(sid)["messages"]) == 1


def test_reopen_and_task_import_preserve_same_message_id(env):
    store, log, sid = env
    task, _ = store.create_task(sid, "query", "task")
    store.finish(task["id"], "completed", "actual result")
    event = store.events(sid, pending=True)[0]
    message = log.sync_task_event(sid, {**event, "data": {"message": "forged caller body"}})
    assert message["text"] == "actual result"
    reopened = MessageLog(store)
    assert reopened.sync_task_event(sid, event["id"]) == message
    reopened.sync_events(sid)
    assert reopened.snapshot(sid)["messages"] == [message]
    assert len(reopened.events(sid)["events"]) == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": True}, {"limit": 501}, {"before": -1}])
def test_invalid_pagination(env, params):
    _, log, sid = env
    with pytest.raises(ValueError):
        log.snapshot(sid, **params)


def test_snapshot_process_history_is_scoped_ordered_capped_and_not_audio(env):
    store, log, sid = env
    other = store.create_session()
    expected = []
    for index in range(205):
        expected.append(log.emit_event(sid, "voice.process", {
            "device_id": "phone", "device_name": "Phone", "step": {
                "id": f"step-{index}", "title": "回复", "detail": f"实际播报 {index}", "status": "success"},
        }))
        log.emit_event(other, "voice.process", {"step": "other conversation"})
    log.emit_event(sid, "voice.audio", {"audio": "not process history"})
    first = log.append(sid, "one", "user", "first", "text")
    log.append(sid, "two", "user", "second", "text")
    snapshot = log.snapshot(sid, limit=1)
    assert snapshot["process_events"] == expected[-200:]
    assert all(event["seq"] <= snapshot["cursor"] for event in snapshot["process_events"])
    assert all("session_id" not in event and "message" not in event for event in snapshot["process_events"])
    older_page = log.snapshot(sid, before=snapshot["next_before"], limit=1)
    assert older_page["messages"] == [first]
    assert older_page["process_events"] == snapshot["process_events"]
    assert log.events(sid, snapshot["cursor"])["events"] == []


def test_process_payload_keeps_spoken_detail_but_excludes_audio_and_credentials(env):
    store, log, sid = env
    payload = {"device_id": "phone", "token": "private", "audio": "binary", "step": {
        "id": "reply", "detail": "确实播报的文字", "audio": "buffer", "api_key": "private", "status": "success"}}
    event = log.emit_event(sid, "voice.process", payload)
    assert event["payload"] == {"device_id": "phone", "step": {
        "id": "reply", "detail": "确实播报的文字", "status": "success"}}
    with store.connect() as db:
        assert "private" not in db.execute("SELECT payload FROM shared_message_events WHERE seq=?", (event["seq"],)).fetchone()[0]
        # Existing rows also go through the same safe public projection.
        db.execute("UPDATE shared_message_events SET payload=? WHERE seq=?",
                   ('{"step":{"detail":"old spoken text","audio":"old buffer"},"token":"old private"}', event["seq"]))
    old = log.snapshot(sid)["process_events"][0]
    assert old["payload"] == {"step": {"detail": "old spoken text"}}
    assert log.events(sid)["events"] == [old]


def test_process_snapshot_cursor_is_consistent_with_concurrent_emit(env, monkeypatch):
    _, log, sid = env
    previous = log.emit_event(sid, "voice.process", {"step": "before snapshot"})
    anchored, release = Event(), Event()
    original = log._cursor

    def hold(db):
        cursor = original(db)
        anchored.set()
        assert release.wait(timeout=5)
        return cursor

    monkeypatch.setattr(log, "_cursor", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(log.snapshot, sid)
        try:
            assert anchored.wait(timeout=5)
            arriving = log.emit_event(sid, "voice.process", {"step": "during snapshot"})
        finally:
            release.set()
        snapshot = read.result(timeout=10)
    assert snapshot["process_events"] == [previous]
    assert snapshot["cursor"] == previous["seq"]
    monkeypatch.setattr(log, "_cursor", original)
    assert log.events(sid, snapshot["cursor"])["events"] == [arriving]


@pytest.mark.parametrize("action", ["edit", "pause", "cancel_plan"])
@pytest.mark.parametrize("sync_before", [True, False])
def test_plan_old_feedback_superseded_on_revision_change(env, action, sync_before):
    store, log, sid = env
    plans = RecurringPlans(store, timezone="UTC")
    plan, _ = plans.create(sid, "plan", "query", {"frequency": "interval", "seconds": 120}, now=1000)
    user = log.append(sid, "request", "user", "query periodically", "text", plan_id=plan["id"])
    plans.due("mock", "mock", "off", "agent", now=1181)
    plans.due("mock", "mock", "off", "agent", now=1240)
    plans.due("mock", "mock", "off", "agent", now=1360)
    if sync_before:
        log.sync_events(sid)
    before_cursor = log.snapshot(sid)["cursor"]
    old_text = {event["id"]: event["data"]["message"] for event in plans.events(sid)
                if event["kind"] in {"plan.missed", "plan.skipped"}}
    plans.update(sid, plan["id"], 0, action, now=1361, **({"goal": "new goal"} if action == "edit" else {}))
    source_notices = plans.events(sid)
    log.sync_events(sid)
    messages = log.snapshot(sid)["messages"]
    assert messages[0] == user
    feedback = [message for message in messages if message["kind"] in {"plan.missed", "plan.skipped"}]
    assert len(feedback) == 2
    for message in feedback:
        assert message["superseded"] and message["superseded_reason"] == "plan_revision_changed"
        assert message["text"] == old_text[message["raw_source"]["id"]]
        assert message["plan_revision"] == 0
        assert message["version"] == (2 if sync_before else 1)
    assert plans.events(sid) == source_notices  # Reading/syncing never acknowledges delivery.
    assert log.sync_events(sid)["messages"] == []
    deltas = log.events(sid, before_cursor)["events"]
    assert all(event["message"]["superseded"] for event in deltas
               if event["message"]["kind"] in {"plan.missed", "plan.skipped"})


@pytest.mark.parametrize("state", ["paused", "cancelled"])
def test_plan_feedback_reconciles_inactive_state_even_without_new_events(env, state):
    store, log, sid = env
    plans = RecurringPlans(store, timezone="UTC")
    plan, _ = plans.create(sid, "plan", "query", {"frequency": "interval", "seconds": 120}, now=1000)
    plans.due("mock", "mock", "off", "agent", now=1181)
    message = log.sync_events(sid)["messages"][0]
    with store.connect() as db:
        db.execute("UPDATE task_plans SET status=?,next_run_at=NULL WHERE id=?", (state, plan["id"]))
    updated = log.sync_events(sid)["messages"][0]
    assert updated["id"] == message["id"] and updated["superseded_reason"] == "plan_" + state
    assert updated["text"] == message["text"]
    assert log.update(sid, updated["id"], superseded=False)["superseded"]
