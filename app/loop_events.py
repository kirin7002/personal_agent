"""Local Demo Inbox and wait registry, using Store.events as the Outbox.

No scheduler, model calls, transport, or restart recovery lives here. The parent
stops the old run before starting a returned resumed task (run_id is preserved).
External adapters push results; pending callbacks do not schedule polls or runs.
User-answer waits are projected from Store's persisted awaiting_input/question;
only timer and external_result waits are stored in loop_waits. Store.ask/resume
remain the sole writers of question state.
Inbox acceptance is separate from dispatch: callers mark_handled after dispatch.
drain_ready atomically claims terminal-task followups for the in-process worker;
there is deliberately no crash/retry delivery protocol.
"""
import json
import math
import time

from app.store import Store, TERMINAL


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected finite JSON data") from exc


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _revision(value):
    if type(value) is not int or value < 0:
        raise ValueError("revision must be a nonnegative integer")


def _number(value, name):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and nonnegative")
    return float(value)


class LoopEvents:
    MAX_WAITS = 3

    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS loop_inbox(
                    session_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    kind TEXT NOT NULL, task_id TEXT, revision INTEGER,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL, handled REAL,
                    PRIMARY KEY(session_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS loop_inbox_pending
                    ON loop_inbox(status, kind, task_id);
                CREATE TABLE IF NOT EXISTS loop_waits(
                    task_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, wait_id TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    deadline REAL, result_ref TEXT, continuation TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    result_status TEXT, result_payload TEXT,
                    PRIMARY KEY(task_id, wait_id)
                );
                CREATE INDEX IF NOT EXISTS loop_waits_pending
                    ON loop_waits(status, deadline);
            """)

    def _task(self, db, tid, sid):
        return self.store.decode(db.execute(
            "SELECT * FROM tasks WHERE id=? AND session_id=?", (tid, sid)
        ).fetchone())

    @staticmethod
    def _record(row):
        result = dict(row)
        for key in ("payload", "result_payload"):
            if key in result and result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def accept(self, sid: str, event_id: str, kind: str,
               task_id: str | None, revision: int | None,
               payload: dict) -> tuple[dict, bool]:
        """Accept once per session/event ID; replay bypasses only the current CAS."""
        for value, name in ((sid, "sid"), (event_id, "event_id"), (kind, "kind")):
            _text(value, name)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        encoded = _json(payload)
        if task_id is None:
            if revision is not None or kind == "append_followup":
                raise ValueError("A task and revision are required for followup")
        else:
            _text(task_id, "task_id")
            _revision(revision)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise KeyError("Session not found")
            task = self._task(db, task_id, sid) if task_id is not None else None
            old = db.execute("SELECT * FROM loop_inbox WHERE session_id=? AND event_id=?",
                             (sid, event_id)).fetchone()
            if old:
                if (old["kind"], old["task_id"], old["revision"], old["payload"]) != (
                        kind, task_id, revision, encoded):
                    raise ValueError("Event ID conflicts with an earlier event")
                return self._record(old), False
            if task is not None and task["revision"] != revision:
                raise ValueError("Task revision changed")
            db.execute("""INSERT INTO loop_inbox
                (session_id,event_id,kind,task_id,revision,payload,created)
                VALUES(?,?,?,?,?,?,?)""",
                       (sid, event_id, kind, task_id, revision, encoded, time.time()))
            return self._record(db.execute(
                "SELECT * FROM loop_inbox WHERE session_id=? AND event_id=?",
                (sid, event_id)).fetchone()), True

    def mark_handled(self, sid: str, event_id: str) -> bool:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM loop_inbox WHERE session_id=? AND event_id=?",
                             (sid, event_id)).fetchone()
            if row is None:
                raise KeyError("Inbox event not found")
            if row["status"] == "handled":
                return False
            db.execute("UPDATE loop_inbox SET status='handled',handled=? WHERE session_id=? AND event_id=?",
                       (time.time(), sid, event_id))
            return True

    def drain_ready(self, sid: str | None = None) -> list[dict]:
        """Claim followups once, only after their original task is terminal.

        A followup remains attached to the original task across goal revisions.
        This returns input records; the parent creates linked tasks sequentially.
        Chat/thanks events are never returned and never start a model here.
        """
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT i.*, t.status AS task_status FROM loop_inbox i
                JOIN tasks t ON t.id=i.task_id AND t.session_id=i.session_id
                WHERE i.status='pending' AND i.kind='append_followup'
                AND (? IS NULL OR i.session_id=?) ORDER BY i.created,i.rowid""",
                              (sid, sid)).fetchall()
            ready = []
            for row in rows:
                if row["task_status"] not in TERMINAL:
                    continue
                handled = time.time()
                db.execute("UPDATE loop_inbox SET status='handled',handled=? WHERE session_id=? AND event_id=?",
                           (handled, row["session_id"], row["event_id"]))
                record = self._record(row)
                record.pop("task_status")
                record.update(status="handled", handled=handled)
                ready.append(record)
            return ready

    def register_wait(self, task: dict, kind: str, wait_id: str, *,
                      delay_seconds: float | None = None,
                      result_ref: str | None = None,
                      continuation: str = "") -> dict:
        """Register one active wait at a running revision, with a lifetime budget."""
        _text(wait_id, "wait_id")
        _revision(task["revision"])
        if not isinstance(continuation, str):
            raise ValueError("continuation must be a string")
        if kind == "timer":
            delay_seconds = _number(delay_seconds, "delay_seconds")
            if result_ref is not None:
                raise ValueError("Timer waits cannot have a result_ref")
        elif kind == "external_result":
            _text(result_ref, "result_ref")
            if delay_seconds is not None:
                raise ValueError("External waits cannot have a delay")
        else:
            raise ValueError("Wait kind must be timer or external_result")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._task(db, task["id"], task["session_id"])
            deadline = None
            if kind == "timer":
                deadline = _number(current["created"], "task.created") + delay_seconds
                _number(deadline, "deadline")
            old = db.execute("SELECT * FROM loop_waits WHERE task_id=? AND wait_id=?",
                             (current["id"], wait_id)).fetchone()
            if old:
                if (old["revision"], old["kind"], old["deadline"], old["result_ref"], old["continuation"]) != (
                        task["revision"], kind, deadline, result_ref, continuation):
                    raise ValueError("Wait ID conflicts with an earlier wait")
                return self._record(old)
            if current["status"] != "running" or current["revision"] != task["revision"]:
                raise ValueError("Wait requires the current running task revision")
            count = db.execute("SELECT count(*) FROM loop_waits WHERE task_id=?",
                               (current["id"],)).fetchone()[0]
            if count >= self.MAX_WAITS:
                raise ValueError("Task wait budget exhausted (maximum 3)")
            now = time.time()
            db.execute("""INSERT INTO loop_waits
                (task_id,session_id,revision,wait_id,kind,deadline,result_ref,continuation,created,updated,result_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (current["id"], current["session_id"], current["revision"], wait_id,
                        kind, deadline, result_ref, continuation, now, now,
                        "pending" if kind == "external_result" else None))
            db.execute("""UPDATE tasks SET status='waiting',outcome='pending',
                summary=NULL,final_result=NULL,updated=? WHERE id=?""", (now, current["id"]))
            wait = self._record(db.execute("SELECT * FROM loop_waits WHERE task_id=? AND wait_id=?",
                                          (current["id"], wait_id)).fetchone())
            self.store._event(db, current, "task.waiting", {
                "message": "已登记等待，条件满足后继续原任务", "status": "waiting",
                "outcome": "pending", "wait_id": wait_id, "kind": kind,
                "deadline": deadline, "result_ref": result_ref, "continuation": continuation,
            }, True)
            return wait

    def waits(self, tid: str, sid: str) -> list[dict]:
        """Return registered waits and the current persisted user-question wait.

        Question records are read-only projections, not entries in loop_waits;
        they neither consume the timer/external budget nor change ask/resume.
        """
        with self.store.connect() as db:
            # Keep task and registered-wait reads in one consistent snapshot.
            db.execute("BEGIN")
            task = self._task(db, tid, sid)
            records = [self._record(row) for row in db.execute(
                "SELECT * FROM loop_waits WHERE task_id=? ORDER BY created,rowid", (tid,))]
            if task["status"] == "awaiting_input":
                records.append({
                    "task_id": tid, "session_id": sid, "revision": task["revision"],
                    "wait_id": f"question-{tid}-{task['revision']}", "kind": "user_answer",
                    "status": "pending", "expected_question": task["question"],
                    "deadline": None, "result_ref": None, "continuation": "",
                    "created": task["updated"], "updated": task["updated"],
                    "result_status": None, "result_payload": None,
                })
            return records

    @staticmethod
    def _active(task, wait):
        return task["status"] == "waiting" and task["revision"] == wait["revision"]

    @staticmethod
    def _silence(db, wait):
        # Do not suppress a newer waiting notice while invalidating an older wait.
        rows = db.execute("SELECT id,data FROM events WHERE task_id=? AND kind='task.waiting' AND notify=1",
                          (wait["task_id"],)).fetchall()
        for row in rows:
            if json.loads(row["data"]).get("wait_id") == wait["wait_id"]:
                db.execute("UPDATE events SET notify=0 WHERE id=?", (row["id"],))

    def _invalidate(self, db, wait, now):
        db.execute("UPDATE loop_waits SET status='invalidated',updated=? WHERE task_id=? AND wait_id=?",
                   (now, wait["task_id"], wait["wait_id"]))
        self._silence(db, wait)

    def _resume(self, db, task, wait, status, payload, now):
        entry = {"kind": "timer_due" if wait["kind"] == "timer" else "external_result_ready",
                 "wait_id": wait["wait_id"], "wait_kind": wait["kind"],
                 "task_revision": wait["revision"], "result_ref": wait["result_ref"],
                 "status": status, "payload": payload, "continuation": wait["continuation"],
                 "deadline": wait["deadline"]}
        event_id = f"wait-{task['id']}-{wait['wait_id']}"
        encoded = _json(entry)
        old = db.execute("SELECT * FROM loop_inbox WHERE session_id=? AND event_id=?",
                         (task["session_id"], event_id)).fetchone()
        if old:
            if (old["kind"], old["task_id"], old["revision"], old["payload"]) != (
                    entry["kind"], task["id"], wait["revision"], encoded):
                raise ValueError("Resume event ID conflicts with an earlier event")
            db.execute("UPDATE loop_inbox SET status='handled',handled=? WHERE session_id=? AND event_id=?",
                       (now, task["session_id"], event_id))
        else:
            db.execute("""INSERT INTO loop_inbox
                (session_id,event_id,kind,task_id,revision,payload,status,created,handled)
                VALUES(?,?,?,?,?,?,'handled',?,?)""",
                       (task["session_id"], event_id, entry["kind"], task["id"],
                        wait["revision"], encoded, now, now))
        db.execute("""UPDATE loop_waits SET status='consumed',result_status=?,result_payload=?,updated=?
            WHERE task_id=? AND wait_id=?""",
                   (status, _json(payload), now, task["id"], wait["wait_id"]))
        db.execute("""UPDATE tasks SET status='running',revision=revision+1,history=?,
            outcome=NULL,question=NULL,summary=NULL,final_result=NULL,updated=? WHERE id=?""",
                   (_json([*task["history"], entry]), now, task["id"]))
        self._silence(db, wait)
        resumed = self._task(db, task["id"], task["session_id"])
        self.store._event(db, resumed, "task.resumed", {
            **entry, "task_revision": resumed["revision"],
            "waiting_revision": wait["revision"], "message": "等待条件已满足，继续原任务",
        })
        return resumed

    def consume_due(self, *, now: float | None = None) -> list[dict]:
        now = _number(time.time() if now is None else now, "now")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT * FROM loop_waits WHERE status='pending' ORDER BY deadline,created,rowid").fetchall()
            resumed = []
            for wait in pending:
                task = self._task(db, wait["task_id"], wait["session_id"])
                if not self._active(task, wait):
                    self._invalidate(db, wait, now)
                elif wait["kind"] == "timer" and wait["deadline"] <= now:
                    resumed.append(self._resume(db, task, wait, "done", {}, now))
            return resumed

    def resume_external_result(self, tid: str, sid: str, wait_id: str,
                               result_ref: str, status: str, payload: dict) -> dict | None:
        """An adapter supplies pending/done/failed; done and failed both resume.

        A failed external operation is evidence for the next agent turn, not a
        fabricated successful task. Identical terminal callbacks are no-ops;
        contradictory terminal callbacks conflict. Late pending is ignored.
        """
        if status not in ("pending", "done", "failed"):
            raise ValueError("Result status must be pending, done or failed")
        _text(wait_id, "wait_id")
        _text(result_ref, "result_ref")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        encoded = _json(payload)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, tid, sid)
            wait = db.execute("SELECT * FROM loop_waits WHERE task_id=? AND wait_id=?",
                              (tid, wait_id)).fetchone()
            if wait is None:
                raise KeyError("Wait not found")
            if wait["kind"] != "external_result" or wait["result_ref"] != result_ref:
                raise ValueError("Result does not match the external wait reference")
            if wait["status"] == "consumed":
                if status != "pending" and (wait["result_status"], wait["result_payload"]) != (status, encoded):
                    raise ValueError("External result conflicts with the consumed result")
                return None
            if wait["status"] == "invalidated":
                return None
            now = time.time()
            if not self._active(task, wait):
                self._invalidate(db, wait, now)
                return None
            if status == "pending":
                db.execute("""UPDATE loop_waits SET result_status='pending',result_payload=?,updated=?
                    WHERE task_id=? AND wait_id=?""", (encoded, now, tid, wait_id))
                return None
            return self._resume(db, task, wait, status, payload, now)

    def cancel_waits(self, tid: str, sid: str, expected_revision: int | None = None) -> int:
        """Invalidate waits after parent cancellation/revision, never orphan a live wait.

        The caller normally invokes store.cancel first. Even if a callback runs
        between these calls it sees the terminal state and cannot revive it.
        """
        if expected_revision is not None:
            _revision(expected_revision)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, tid, sid)
            if expected_revision is not None and task["revision"] != expected_revision:
                raise ValueError("Task revision changed")
            pending = db.execute("SELECT * FROM loop_waits WHERE task_id=? AND status='pending'",
                                 (tid,)).fetchall()
            if any(self._active(task, wait) for wait in pending):
                raise ValueError("Cancel or revise the task before invalidating its active wait")
            for wait in pending:
                self._invalidate(db, wait, time.time())
            return len(pending)
