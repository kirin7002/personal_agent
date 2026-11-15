"""Canonical conversation messages and a separate, replayable SSE event log.

Message IDs order history; event sequences order every upsert/process delta
across the database. snapshot/events each read a consistent SQLite snapshot.
Snapshots include the newest 200 voice.process metadata events in ascending seq
order, independently of the message page, at the same cursor/snapshot boundary.
Readers never acknowledge Store/plan spoken flags. The parent authenticates the
device and conversation for every read/write and checks long-lived connections.

append is idempotent against its ORIGINAL request even after a final transcript
update. update optionally checks expected_version; None means last writer wins.
Event keys are reserved: event:<task-event-id>, plan_event:<plan-event-id>.
Source events are read from SQLite, not trusted from caller-supplied dictionaries.
emit_event is for metadata such as voice.process, not audio or credentials.
"""
import json
import math
import time

from app.store import TERMINAL


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected finite JSON data") from exc


def _string(value, name, *, empty=False, maximum=65536):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"Invalid {name}")
    return value


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid {name}")
    return value


def _process_payload(payload):
    """Expose only process UI metadata, including the spoken text in detail.

    Audio buffers and credentials do not belong in this stream. Apply this to
    writes and reads so legacy payloads cannot leak extra fields on refresh.
    """
    scalar = lambda value: value is None or type(value) in (str, int, float, bool)
    public = {key: payload[key] for key in ("device_id", "device_name", "task_id", "plan_id", "connection_id")
              if key in payload and scalar(payload[key])}
    step = payload.get("step")
    if isinstance(step, dict):
        public["step"] = {key: step[key] for key in (
            "connection_id", "id", "title", "detail", "status", "started_at", "duration_ms", "task_id", "plan_id")
            if key in step and scalar(step[key])}
    elif "step" in payload and scalar(step):
        public["step"] = step
    return public


class MessageLog:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    key TEXT NOT NULL, original_request TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
                    device_id TEXT, task_id TEXT, plan_id TEXT, kind TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1, final INTEGER NOT NULL DEFAULT 1,
                    superseded INTEGER NOT NULL DEFAULT 0, superseded_reason TEXT,
                    task_revision INTEGER, task_status TEXT, plan_revision INTEGER,
                    raw_source TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(session_id,key)
                );
                CREATE INDEX IF NOT EXISTS shared_messages_session ON shared_messages(session_id,id);
                CREATE TABLE IF NOT EXISTS shared_message_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, message_id INTEGER, message_version INTEGER,
                    payload TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(message_id,message_version)
                );
                CREATE INDEX IF NOT EXISTS shared_message_events_session ON shared_message_events(session_id,seq);
                CREATE TABLE IF NOT EXISTS shared_message_sync(
                    session_id TEXT PRIMARY KEY, task_cursor INTEGER NOT NULL DEFAULT 0,
                    plan_cursor INTEGER NOT NULL DEFAULT 0
                );
            """)

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite nonnegative epoch")
        return float(now)

    @staticmethod
    def _exists(db, table):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    @staticmethod
    def _session(db, sid):
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise KeyError("Conversation not found")

    @staticmethod
    def _public(row):
        result = dict(row)
        result.pop("session_id", None)
        result.pop("original_request", None)
        result["raw_source"] = json.loads(result["raw_source"]) if result["raw_source"] else None
        result["final"] = bool(result["final"])
        result["superseded"] = bool(result["superseded"])
        return result

    def _get(self, db, sid, message_id):
        row = db.execute("SELECT * FROM shared_messages WHERE id=? AND session_id=?", (message_id, sid)).fetchone()
        if row is None:
            raise KeyError("Message not found")
        return self._public(row)

    def _links(self, db, sid, device_id, task_id, plan_id):
        metadata = {"task_revision": None, "task_status": None, "plan_revision": None}
        if device_id is not None:
            if not self._exists(db, "shared_devices") or not db.execute(
                    "SELECT 1 FROM shared_devices WHERE id=? AND session_id=? AND revoked_at IS NULL AND trusted=1",
                    (device_id, sid)).fetchone():
                raise KeyError("Device not bound to conversation")
        if task_id is not None:
            task = db.execute("SELECT revision,status FROM tasks WHERE id=? AND session_id=?", (task_id, sid)).fetchone()
            if task is None:
                raise KeyError("Task not found in conversation")
            metadata.update(task_revision=task["revision"], task_status=task["status"])
        if plan_id is not None:
            row = db.execute("SELECT revision FROM task_plans WHERE id=? AND session_id=?", (plan_id, sid)).fetchone() if self._exists(db, "task_plans") else None
            if row is None:
                raise KeyError("Plan not found in conversation")
            metadata["plan_revision"] = row[0]
        return metadata

    @staticmethod
    def _obsolete(db, message):
        if message["role"] == "user":
            return None
        if message["plan_id"] and message["kind"] in {"plan.skipped", "plan.missed"}:
            plan = db.execute("SELECT revision,status FROM task_plans WHERE id=?", (message["plan_id"],)).fetchone() if MessageLog._exists(db, "task_plans") else None
            if plan is None:
                return "plan_unavailable"
            if plan["revision"] != message["plan_revision"]:
                return "plan_revision_changed"
            if plan["status"] in {"paused", "cancelled"}:
                return "plan_" + plan["status"]
        if not message["task_id"]:
            return None
        task = db.execute("SELECT revision,status,question FROM tasks WHERE id=?", (message["task_id"],)).fetchone()
        if task is None:
            return "task_unavailable"
        if task["revision"] != message["task_revision"]:
            return "task_revision_changed"
        if message["kind"] == "task.question":
            raw = message.get("raw_source")
            raw = json.loads(raw) if isinstance(raw, str) else raw
            original_question = raw.get("data", {}).get("message", message["text"]) if raw else message["text"]
            if task["status"] != "awaiting_input" or task["question"] != original_question:
                return "question_no_longer_current"
        if task["status"] in {"cancelled", "expired"} and task["status"] != message["task_status"]:
            return "task_" + task["status"]
        if message["source"] == "task" and message["kind"].removeprefix("task.") in TERMINAL and task["status"] != message["task_status"]:
            return "task_status_changed"
        return None

    @staticmethod
    def _cursor(db):
        return db.execute("SELECT coalesce(max(seq),0) FROM shared_message_events").fetchone()[0]

    def _upsert_event(self, db, sid, message):
        db.execute("""INSERT INTO shared_message_events
            (session_id,kind,message_id,message_version,payload,created) VALUES(?,'message.upsert',?,?,?,?)""",
                   (sid, message["id"], message["version"], _json(message), self._now()))

    def _append(self, db, sid, key, role, text, source, device_id=None, task_id=None,
                plan_id=None, kind="chat", *, final=True, raw_source=None, metadata=None, created=None):
        request = _json(dict(role=role, text=text, source=source, device_id=device_id,
                             task_id=task_id, plan_id=plan_id, kind=kind, final=final, raw_source=raw_source))
        old = db.execute("SELECT * FROM shared_messages WHERE session_id=? AND key=?", (sid, key)).fetchone()
        if old:
            if old["original_request"] != request:
                raise ValueError("Message key conflicts with an earlier request")
            return self._public(old), False
        links = self._links(db, sid, device_id, task_id, plan_id)
        if metadata:
            links.update(metadata)
        now = self._now()
        values = dict(session_id=sid, key=key, original_request=request, role=role, text=text,
                      source=source, device_id=device_id, task_id=task_id, plan_id=plan_id,
                      kind=kind, final=int(final), raw_source=_json(raw_source) if raw_source is not None else None,
                      created=now if created is None else created, updated=now, **links)
        reason = self._obsolete(db, values)
        values.update(superseded=int(reason is not None), superseded_reason=reason)
        inserted = db.execute(f"INSERT INTO shared_messages ({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
        message = self._get(db, sid, inserted.lastrowid)
        self._upsert_event(db, sid, message)
        return message, True

    def append(self, sid, key, role, text, source, device_id=None, task_id=None, plan_id=None,
               kind="chat", *, final=True, raw_source=None):
        _string(key, "key", maximum=256)
        if key.startswith(("event:", "plan_event:")):
            raise ValueError("Source event message keys are reserved")
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("Invalid message role")
        _string(text, "text", empty=True)
        _string(source, "source", maximum=128)
        _string(kind, "kind", maximum=128)
        if type(final) is not bool:
            raise ValueError("final must be boolean")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._session(db, sid)
            # Validate a supplied device even on replay, so revoked/rebound callers
            # cannot bypass ownership through a previously valid idempotency key.
            self._links(db, sid, device_id, task_id, plan_id)
            return self._append(db, sid, key, role, text, source, device_id, task_id, plan_id, kind,
                                final=final, raw_source=raw_source)[0]

    def by_key(self, sid, key):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM shared_messages WHERE session_id=? AND key=?", (sid, key)).fetchone()
            return self._public(row) if row else None

    def _change(self, db, sid, message, changes):
        changes = {key: value for key, value in changes.items() if message.get(key) != value}
        if not changes:
            return message
        changes.update(version=message["version"] + 1, updated=self._now())
        db.execute("UPDATE shared_messages SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=? AND session_id=?",
                   (*changes.values(), message["id"], sid))
        updated = self._get(db, sid, message["id"])
        self._upsert_event(db, sid, updated)
        return updated

    def _update(self, db, sid, message_id, expected_version, *, text=None, task_id=None,
                plan_id=None, kind=None, final=None, superseded=None):
        message = self._get(db, sid, message_id)
        if expected_version is not None:
            _integer(expected_version, "expected_version", 1)
            if message["version"] != expected_version:
                raise ValueError("Message version changed")
        changes = {}
        if text is not None:
            changes["text"] = _string(text, "text", empty=True)
        if kind is not None:
            changes["kind"] = _string(kind, "kind", maximum=128)
        for field, value in (("final", final), ("superseded", superseded)):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(f"{field} must be boolean")
                changes[field] = int(value)
        if task_id is not None and task_id != message["task_id"]:
            links = self._links(db, sid, None, task_id, None)
            changes.update(task_id=task_id, task_revision=links["task_revision"], task_status=links["task_status"])
        if plan_id is not None and plan_id != message["plan_id"]:
            links = self._links(db, sid, None, None, plan_id)
            changes.update(plan_id=plan_id, plan_revision=links["plan_revision"])
        reason = self._obsolete(db, {**message, **changes})
        if reason:
            changes.update(superseded=1, superseded_reason=reason)
        elif superseded is not None:
            changes["superseded_reason"] = "explicit" if superseded else None
        return self._change(db, sid, message, changes)

    def update(self, sid, message_id, expected_version=None, **changes):
        _integer(message_id, "message_id", 1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._update(db, sid, message_id, expected_version, **changes)

    def update_by_key(self, sid, key, expected_version=None, **changes):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM shared_messages WHERE session_id=? AND key=?", (sid, key)).fetchone()
            if row is None:
                raise KeyError("Message not found")
            return self._update(db, sid, row[0], expected_version, **changes)

    def snapshot(self, sid, before=None, limit=100):
        _integer(limit, "limit", 1, 500)
        if before is not None:
            _integer(before, "before", 1)
        with self.store.connect() as db:
            db.execute("BEGIN")
            cursor = self._cursor(db)
            rows = db.execute("SELECT * FROM shared_messages WHERE session_id=?" + (" AND id<?" if before is not None else "") +
                              " ORDER BY id DESC LIMIT ?", (sid, before, limit+1) if before is not None else (sid, limit+1)).fetchall()
            messages = [self._public(row) for row in reversed(rows[:limit])]
            process_rows = db.execute("""SELECT * FROM shared_message_events
                WHERE session_id=? AND kind='voice.process' AND seq<=?
                ORDER BY seq DESC LIMIT 200""", (sid, cursor)).fetchall()
            return {"messages": messages, "cursor": cursor, "has_more": len(rows) > limit,
                    "next_before": messages[0]["id"] if len(rows) > limit else None,
                    "process_events": [self._event_public(row) for row in reversed(process_rows)]}

    @staticmethod
    def _event_public(row):
        result = {"id": row["seq"], "seq": row["seq"], "kind": row["kind"], "created": row["created"]}
        payload = json.loads(row["payload"])
        if row["kind"] == "voice.process":
            payload = _process_payload(payload)
        result["message" if row["kind"] == "message.upsert" else "payload"] = payload
        return result

    def events(self, sid, after=0, limit=200):
        _integer(after, "after")
        _integer(limit, "limit", 1, 1000)
        with self.store.connect() as db:
            db.execute("BEGIN")
            head = self._cursor(db)
            rows = db.execute("SELECT * FROM shared_message_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (sid, after, limit+1)).fetchall()
            events = [self._event_public(row) for row in rows[:limit]]
            more = len(rows) > limit
            return {"events": events, "cursor": events[-1]["seq"] if more else max(after, head), "has_more": more}

    def emit_event(self, sid, kind, payload):
        _string(kind, "kind", maximum=128)
        if kind == "message.upsert" or not isinstance(payload, dict):
            raise ValueError("Process events require an object and a non-message kind")
        encoded = _json(_process_payload(payload) if kind == "voice.process" else payload)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._session(db, sid)
            seq = db.execute("INSERT INTO shared_message_events(session_id,kind,payload,created) VALUES(?,?,?,?)",
                             (sid, kind, encoded, self._now())).lastrowid
            return self._event_public(db.execute("SELECT * FROM shared_message_events WHERE seq=?", (seq,)).fetchone())

    def _import(self, db, sid, stream, row):
        kind, data = row["kind"], json.loads(row["data"])
        if stream == "task":
            if kind != "task.question" and kind not in {"task." + status for status in TERMINAL}:
                return None, False
            task = db.execute("SELECT * FROM tasks WHERE id=? AND session_id=?", (row["task_id"], sid)).fetchone()
            if task is None:
                return None, False
            text = data.get("message")
            if not isinstance(text, str) or not text:
                return None, False
            key, task_id, plan_id = "event:" + str(row["id"]), row["task_id"], None
            revision = data.get("task_revision", 0)
            metadata = {"task_revision": revision, "task_status": "awaiting_input" if kind == "task.question" else kind[5:]}
        else:
            if kind not in {"plan.missed", "plan.skipped"}:
                return None, False
            text = data.get("message")
            if not isinstance(text, str) or not text:
                return None, False
            key, task_id, plan_id = "plan_event:" + str(row["id"]), None, row["plan_id"]
            metadata = {"plan_revision": row["plan_revision"]}
        raw = {"stream": stream, "id": row["id"], "kind": kind, "data": data, "created": row["created"]}
        return self._append(db, sid, key, "assistant", text, stream, task_id=task_id, plan_id=plan_id,
                            kind=kind, raw_source=raw, metadata=metadata, created=row["created"])

    def _reconcile(self, db, sid):
        changed = []
        rows = db.execute("""SELECT * FROM shared_messages WHERE session_id=?
            AND (task_id IS NOT NULL OR plan_id IS NOT NULL) AND superseded=0 AND role!='user'""", (sid,)).fetchall()
        for row in rows:
            message = self._public(row)
            reason = self._obsolete(db, message)
            if reason:
                changed.append(self._change(db, sid, message, {"superseded": 1, "superseded_reason": reason}))
        return changed

    def sync_task_event(self, sid, event_id_or_row):
        event_id = event_id_or_row["id"] if isinstance(event_id_or_row, dict) else event_id_or_row
        _integer(event_id, "event_id", 1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM events WHERE id=? AND session_id=?", (event_id, sid)).fetchone()
            if row is None:
                raise KeyError("Event not found in conversation")
            message, _ = self._import(db, sid, "task", row)
            self._reconcile(db, sid)
            return self._get(db, sid, message["id"]) if message else None

    def sync_events(self, sid):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._session(db, sid)
            db.execute("INSERT OR IGNORE INTO shared_message_sync(session_id) VALUES(?)", (sid,))
            cursors = db.execute("SELECT * FROM shared_message_sync WHERE session_id=?", (sid,)).fetchone()
            tasks = db.execute("SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id", (sid, cursors["task_cursor"])).fetchall()
            plans = db.execute("SELECT * FROM plan_events WHERE session_id=? AND id>? ORDER BY id", (sid, cursors["plan_cursor"])).fetchall() if self._exists(db, "plan_events") else []
            sources = [("task", row) for row in tasks] + [("plan", row) for row in plans]
            changed = {}
            for stream, row in sorted(sources, key=lambda pair: (pair[1]["created"], pair[0], pair[1]["id"])):
                message, created = self._import(db, sid, stream, row)
                if created:
                    changed[message["id"]] = message
            for message in self._reconcile(db, sid):
                changed[message["id"]] = message
            db.execute("UPDATE shared_message_sync SET task_cursor=?,plan_cursor=? WHERE session_id=?",
                       (tasks[-1]["id"] if tasks else cursors["task_cursor"], plans[-1]["id"] if plans else cursors["plan_cursor"], sid))
            return {"messages": list(changed.values()), "cursor": self._cursor(db)}
