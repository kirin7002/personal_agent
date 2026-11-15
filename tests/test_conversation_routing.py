"""Routing/expiry contracts against the real SQLite Store, without model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest

from app.conversation_routing import ConversationRouter, expire_in_transaction, is_cancellation
from app.store import Store


@pytest.fixture
def context(tmp_path):
    store = Store(tmp_path / "routing.sqlite3")
    sid = store.create_session()
    return store, ConversationRouter(store), sid


def create(store, sid, goal="打开车窗", key="one", question=None):
    task, _ = store.create_task(sid, goal, key)
    if question:
        store.ask(task["id"], question)
    return store.task(task["id"])


def question_time(store, tid, timestamp, revision=0):
    with store.connect() as db:
        rows = db.execute("SELECT id,data FROM events WHERE task_id=? AND kind='task.question'", (tid,)).fetchall()
        for row in rows:
            if json.loads(row["data"])["task_revision"] == revision:
                db.execute("UPDATE events SET created=? WHERE id=?", (timestamp, row["id"]))


def test_user_focus_is_not_background_update_order(context):
    store, router, sid = context
    old = create(store, sid, question="哪个车窗？")
    new = create(store, sid, "空调调到23度", "two", "哪个分区？")
    router.focus(sid, new["id"])
    store.set_run(old["id"], "background-heartbeat")
    store.event(old["id"], "task.progress", {"message": "仍在等待"})
    assert router.current(sid)["id"] == new["id"]
    route = router.resolve(sid, "全部")
    assert route == {"kind": "answer_question", "task_id": new["id"],
                     "task_revision": 0, "text": "全部", "explicit": False}
    assert store.task(new["id"])["status"] == "awaiting_input"
    assert len(store.tasks(sid)) == 2


@pytest.mark.parametrize("status", ["running", "awaiting_input", "waiting"])
def test_current_accepts_only_active_states(context, status):
    store, router, sid = context
    task = create(store, sid)
    with store.connect() as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))
    router.focus(sid, task["id"])
    assert router.current(sid)["status"] == status


@pytest.mark.parametrize("status", ["completed", "generated", "cancelled", "expired", "failed", "unsupported", "interrupted", "partial"])
def test_terminal_focus_keeps_tombstone_and_never_falls_back(context, status):
    store, router, sid = context
    create(store, sid, question="哪个车窗？")
    newer = create(store, sid, "空调调到23度", "two")
    router.focus(sid, newer["id"])
    with store.connect() as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, newer["id"]))
    assert router.current(sid) is None
    assert ConversationRouter(store).resolve(sid, "主驾")["kind"] == "clarify"
    with store.connect() as db:
        assert db.execute("SELECT task_id FROM session_task_focus WHERE sid=?", (sid,)).fetchone()[0] == newer["id"]


def test_bootstrap_uses_created_including_terminal_not_updated(context):
    store, router, sid = context
    old = create(store, sid, question="哪个车窗？")
    new = create(store, sid, "空调调到23度", "two")
    with store.connect() as db:
        db.execute("UPDATE tasks SET created=10,updated=999 WHERE id=?", (old["id"],))
        db.execute("UPDATE tasks SET created=20,updated=20,status='completed' WHERE id=?", (new["id"],))
    assert router.current(sid) is None
    with store.connect() as db:
        db.execute("UPDATE tasks SET created=1000 WHERE id=?", (old["id"],))
    assert router.current(sid) is None


def test_clear_and_empty_bootstrap_are_persistent_tombstones(context):
    store, router, sid = context
    assert router.current(sid) is None
    task = create(store, sid, question="哪个车窗？")
    assert router.current(sid) is None
    router.focus(sid, task["id"])
    router.clear(sid)
    assert ConversationRouter(store).current(sid) is None
    assert router.resolve(sid, "全部")["kind"] == "clarify"
    with store.connect() as db:
        focus = db.execute("SELECT * FROM session_task_focus WHERE sid=?", (sid,)).fetchone()
        assert focus["task_id"] is None and focus["user_event_sequence"] == 2


def test_focus_and_resolve_check_ownership_and_dont_leak(context):
    store, router, sid = context
    other = create(store, store.create_session(), question="哪个车窗？")
    with pytest.raises(KeyError):
        router.focus(sid, other["id"])
    with pytest.raises(KeyError):
        router.resolve(sid, "取消", other["id"])
    assert router.resolve(sid, "之前那个开车窗的任务，取消")["candidates"] == []
    with pytest.raises(KeyError):
        router.clear("missing-session")


@pytest.mark.parametrize("text", ["算了", "不用了", "取消这个任务", " 取消这个任务！ ", "算了，不用了", "取消，取消", "停止这个任务"])
def test_explicit_cancellation_is_a_route_not_a_resume(context, text):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    router.focus(sid, task["id"])
    assert is_cancellation(text)
    route = router.resolve(sid, text)
    assert route["kind"] == "cancel" and route["task_id"] == task["id"]
    assert store.task(task["id"])["status"] == "awaiting_input"
    assert len(store.tasks(sid)) == 1


@pytest.mark.parametrize("text", ["别取消", "不是说取消", "不要取消", "不用取消这个任务", "之前那个开车窗的任务别取消", "我不是要取消"])
def test_negated_cancellation_never_mutates_task(context, text):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    assert not is_cancellation(text)
    assert router.resolve(sid, text)["kind"] == "chat"
    assert store.task(task["id"])["status"] == "awaiting_input"


@pytest.mark.parametrize("text", ["不用全部，主驾就好", "不用全部主驾就好", "四个座位全开最高", "主驾和副驾都开到最高档，后排两个位置也都开到最高档"])
def test_parameter_corrections_and_long_answers_stay_on_question(context, text):
    store, router, sid = context
    task = create(store, sid, "打开座椅加热", question="哪个位置和档位？")
    assert not is_cancellation(text)
    route = router.resolve(sid, text)
    assert route["kind"] == "answer_question" and route["task_id"] == task["id"]


@pytest.mark.parametrize("text", ["取消是不是不执行了", "算了还是主驾", "不用全部", "帮我取消车窗儿童锁"])
def test_cancellation_is_not_substring_matching(text):
    assert not is_cancellation(text)


@pytest.mark.parametrize("status", ["running", "waiting", "completed"])
@pytest.mark.parametrize("text", ["全部", "主驾"])
def test_short_answers_require_focused_question(context, status, text):
    store, router, sid = context
    create(store, sid, question="哪个车窗？")
    current = create(store, sid, "空调调到23度", "two")
    with store.connect() as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, current["id"]))
    router.focus(sid, current["id"])
    assert router.resolve(sid, text)["kind"] == "clarify"


@pytest.mark.parametrize("text", ["主驾", "全部", "改成21度", "算了"])
def test_missing_focus_never_creates_fragment_task(context, text):
    store, router, sid = context
    create(store, sid, question="哪个车窗？")
    router.clear(sid)
    route = router.resolve(sid, text)
    assert route["kind"] == "clarify" and route["task_id"] is None


def test_positive_edit_targets_active_focus_and_revision(context):
    store, router, sid = context
    task = create(store, sid, "空调调到23度")
    with store.connect() as db:
        db.execute("UPDATE tasks SET revision=4 WHERE id=?", (task["id"],))
    router.focus(sid, task["id"])
    assert router.resolve(sid, "改成21度") == {
        "kind": "revise_goal", "task_id": task["id"], "task_revision": 4,
        "text": "改成21度", "explicit": False,
    }


@pytest.mark.parametrize("text", ["之前那个开车窗的任务，取消", "之前开车窗那个，不用了", "之前开车窗那个不用了", "之前那个打开窗户的任务，取消"])
def test_historical_cancel_is_owned_semantic_and_does_not_change_focus(context, text):
    store, router, sid = context
    window = create(store, sid, question="哪个车窗？")
    climate = create(store, sid, "空调调到23度", "two", "哪个分区？")
    create(store, store.create_session(), question="哪个车窗？")
    router.focus(sid, climate["id"])
    route = router.resolve(sid, text)
    assert route["kind"] == "cancel" and route["task_id"] == window["id"]
    assert route["explicit"] and is_cancellation(route["text"])
    assert router.current(sid)["id"] == climate["id"]


def test_history_ambiguity_does_not_prefer_active_or_latest(context):
    store, router, sid = context
    first = create(store, sid, "打开主驾车窗")
    second = create(store, sid, "打开副驾车窗", "two")
    store.finish(first["id"], "completed", "完成")
    router.focus(sid, second["id"])
    route = router.resolve(sid, "之前那个开车窗的任务，取消")
    assert route["kind"] == "clarify"
    assert {t["task_id"] for t in route["candidates"]} == {first["id"], second["id"]}
    route = router.resolve(sid, "之前那个打开主驾车窗的任务，取消")
    assert route["kind"] == "historical" and route["task_id"] == first["id"]


def test_history_action_and_ui_conflict_require_confirmation(context):
    store, router, sid = context
    opened = create(store, sid)
    closed = create(store, sid, "关闭车窗", "two")
    route = router.resolve(sid, "之前开车窗那个，取消", closed["id"])
    assert route["kind"] == "clarify" and route["explicit"]
    assert {t["task_id"] for t in route["candidates"]} == {opened["id"], closed["id"]}
    route = router.resolve(sid, "之前开车窗那个，取消", opened["id"])
    assert route["kind"] == "cancel" and route["text"] == "取消"


def test_explicit_task_overrides_focus_but_only_caller_changes_focus(context):
    store, router, sid = context
    old = create(store, sid, question="哪个车窗？")
    new = create(store, sid, "空调调到23度", "two", "哪个分区？")
    router.focus(sid, new["id"])
    route = router.resolve(sid, "主驾", old["id"])
    assert route["kind"] == "answer_question" and route["task_id"] == old["id"] and route["explicit"]
    assert router.current(sid)["id"] == new["id"]


def test_history_can_find_tasks_older_than_store_task_limit(context):
    store, router, sid = context
    old = create(store, sid)
    for i in range(101):
        create(store, sid, "打开空调", str(i))
    assert old["id"] not in {t["id"] for t in store.tasks(sid)}
    assert router.resolve(sid, "之前开车窗那个，取消")["task_id"] == old["id"]


CATALOG_CASES = [case for filename in ("control-50.json", "queries-18.json", "cross-domain-4.json")
                 for case in json.loads((Path(__file__).parents[1] / "capabilities" / "scenarios" / filename).read_text())]


@pytest.mark.parametrize("case", CATALOG_CASES, ids=lambda c: c["id"])
def test_existing_catalog_full_requests_remain_new_requests(context, case):
    store, router, sid = context
    create(store, sid, question="哪个车窗？")
    route = router.resolve(sid, case["instruction"])
    assert route["kind"] == "new_request" and route["task_id"] is None


@pytest.mark.parametrize("text", ["音量调到5", "开启近光灯", "折叠后视镜", "把空气悬架升高一档", "把天窗打开一半，查看空调温度"])
def test_other_complete_requests_do_not_answer_old_question(context, text):
    store, router, sid = context
    create(store, sid, question="哪个车窗？")
    assert router.resolve(sid, text)["kind"] == "new_request"


@pytest.mark.parametrize("text", ["谢谢", "你好", "今天天气不错", "别取消", "你是谁", "讲个笑话"])
def test_chat_does_not_answer_or_cancel(context, text):
    store, router, sid = context
    create(store, sid, question="哪个车窗？")
    assert router.resolve(sid, text)["kind"] == "chat"


def test_expiry_exact_deadline_clears_question_invalidates_notices_and_is_silent(context):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    router.focus(sid, task["id"])
    started = time.time()
    question_time(store, task["id"], started)
    store.event(task["id"], "task.progress", {"message": "仍在等待"}, notify=True)
    assert router.expire_waiting(now=started + 299.999) == []
    changed = router.expire_waiting(now=started + 300)
    assert len(changed) == 1
    expired = store.task(task["id"])
    assert expired["status"] == "expired" and expired["revision"] == 1 and expired["question"] is None
    assert expired["goal"] == task["goal"] and expired["history"] == task["history"]
    assert router.current(sid) is None
    assert "过期" in router.resolve(sid, "主驾")["message"]
    assert store.events(sid, pending=True) == []
    event = [e for e in store.events(sid) if e["kind"] == "task.expired"][0]
    assert not event["notify"] and event["data"]["task_revision"] == 1
    assert event["data"]["deadline"] == started + 300
    assert router.expire_waiting(now=started + 600) == []


def test_new_question_starts_new_deadline_and_stale_snapshot_cannot_expire_it(context):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    started = time.time()
    question_time(store, task["id"], started)
    store.resume(task["id"], sid, "主驾", expected_revision=0)
    assert router.expire_waiting(now=started + 300) == []
    store.ask(task["id"], "打开多少？", revision=1)
    question_time(store, task["id"], started + 200, revision=1)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert expire_in_transaction(store, db, task, now=started + 900) is None
    assert router.expire_waiting(now=started + 499.999) == []
    assert router.expire_waiting(now=started + 500)[0]["revision"] == 2


def test_heartbeats_duplicate_question_notifications_chat_and_focus_do_not_reset_deadline(context):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    started = time.time()
    question_time(store, task["id"], started)
    store.event(task["id"], "task.question", {"message": task["question"]}, notify=True)
    with store.connect() as db:
        db.execute("UPDATE events SET created=? WHERE id=(SELECT max(id) FROM events)", (started + 200,))
        db.execute("UPDATE tasks SET updated=? WHERE id=?", (started + 299, task["id"]))
    router.focus(sid, task["id"])
    assert router.resolve(sid, "谢谢")["kind"] == "chat"
    assert router.expire_waiting(now=started + 300)[0]["id"] == task["id"]


def test_legacy_question_uses_updated_fallback(context):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    with store.connect() as db:
        db.execute("DELETE FROM events WHERE task_id=? AND kind='task.question'", (task["id"],))
        db.execute("UPDATE tasks SET updated=100 WHERE id=?", (task["id"],))
    assert router.expire_waiting(now=399.99) == []
    assert router.expire_waiting(now=400)[0]["id"] == task["id"]


def test_expiry_is_only_awaiting_input_and_does_not_change_other_session_notifications(context):
    store, router, sid = context
    for status in ("running", "waiting", "completed", "cancelled"):
        task = create(store, sid, key=status)
        with store.connect() as db:
            db.execute("UPDATE tasks SET status=?,updated=0 WHERE id=?", (status, task["id"]))
    other_sid = store.create_session()
    other = create(store, other_sid, question="哪个车窗？")
    waiting = create(store, sid, key="expires", question="哪个车窗？")
    question_time(store, waiting["id"], 0)
    assert [t["id"] for t in router.expire_waiting(now=300)] == [waiting["id"]]
    assert store.events(other_sid, pending=True)[0]["task_id"] == other["id"]


def test_expiring_focus_does_not_restore_older_live_question(context):
    store, router, sid = context
    old = create(store, sid, question="哪个车窗？")
    new = create(store, sid, "空调调到23度", "new", "哪个分区？")
    router.focus(sid, new["id"])
    question_time(store, new["id"], 0)
    router.expire_waiting(now=300)
    assert store.task(old["id"])["status"] == "awaiting_input"
    assert router.current(sid) is None
    assert router.resolve(sid, "全部")["kind"] == "clarify"


def test_helper_uses_existing_transaction_without_commit_or_nested_begin(context):
    store, _, sid = context
    task = create(store, sid, question="哪个车窗？")
    question_time(store, task["id"], 0)
    with pytest.raises(RuntimeError, match="rollback"):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = expire_in_transaction(store, db, task, now=300)
            assert expired["status"] == "expired" and db.in_transaction
            assert expire_in_transaction(store, db, task, now=300) is None
            raise RuntimeError("rollback")
    assert store.task(task["id"])["status"] == "awaiting_input"
    assert not any(e["kind"] == "task.expired" for e in store.events(sid))


def test_two_expiry_workers_emit_one_transition(context):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    question_time(store, task["id"], 0)
    second_router = ConversationRouter(store)
    barrier = threading.Barrier(2)

    def expire(r):
        barrier.wait(timeout=5)
        return r.expire_waiting(now=300)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(expire, [router, second_router]))
    assert sum(len(result) for result in results) == 1
    assert store.task(task["id"])["revision"] == 1
    assert sum(e["kind"] == "task.expired" for e in store.events(sid)) == 1


@pytest.mark.parametrize("operation", ["resume", "revise"])
def test_store_guards_commit_expiry_before_rejecting_late_input(context, monkeypatch, operation):
    store, _, sid = context
    task = create(store, sid, "空调调到23度", question="哪个分区？")
    question_time(store, task["id"], 1000)
    monkeypatch.setattr("app.conversation_routing.time.time", lambda: 1300)
    with pytest.raises(ValueError, match="过期"):
        if operation == "resume":
            store.resume(task["id"], sid, "全部", expected_revision=0)
        else:
            store.revise_goal(task["id"], sid, "改成21度", 0, "late-edit", "空调调到21度", {})
    expired = store.task(task["id"])
    assert expired["status"] == "expired" and expired["revision"] == 1
    assert expired["history"] == [] and expired["goal"] == "空调调到23度"
    assert store.latest_change(task["id"]) is None
    assert sum(e["kind"] == "task.expired" for e in store.events(sid)) == 1


def test_answer_and_expiry_at_deadline_cannot_reopen_task(context, monkeypatch):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    question_time(store, task["id"], 1000)
    monkeypatch.setattr("app.conversation_routing.time.time", lambda: 1300)
    barrier = threading.Barrier(2)

    def expire():
        barrier.wait(timeout=5)
        return router.expire_waiting()

    def answer():
        barrier.wait(timeout=5)
        with pytest.raises(ValueError):
            store.resume(task["id"], sid, "全部", expected_revision=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(expire), pool.submit(answer)]
        for future in futures:
            future.result(timeout=10)
    expired = store.task(task["id"])
    assert expired["status"] == "expired" and expired["revision"] == 1 and expired["history"] == []
    events = store.events(sid)
    assert sum(e["kind"] == "task.expired" for e in events) == 1
    assert not any(e["kind"] == "task.resumed" for e in events)


def test_answer_just_before_deadline_ends_that_question_timer(context, monkeypatch):
    store, router, sid = context
    task = create(store, sid, question="哪个车窗？")
    question_time(store, task["id"], 1000)
    monkeypatch.setattr("app.conversation_routing.time.time", lambda: 1299.999)
    resumed = store.resume(task["id"], sid, "主驾", expected_revision=0)
    assert resumed["status"] == "running" and resumed["revision"] == 1
    assert router.expire_waiting(now=1300) == []


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_timeout_cannot_expire_tasks(context, timeout):
    _, router, _ = context
    with pytest.raises(ValueError):
        router.expire_waiting(timeout=timeout)
