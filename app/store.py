"""Single-user demo persistence shared by the web app and the stdio MCP server."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

TERMINAL = {"generated", "completed", "partial", "failed", "cancelled", "interrupted", "unsupported", "expired"}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL,
              goal TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              source TEXT NOT NULL, calls TEXT NOT NULL DEFAULT '[]',
              question TEXT, summary TEXT, run_id TEXT, history TEXT NOT NULL DEFAULT '[]',
              created REAL NOT NULL, updated REAL NOT NULL,
              UNIQUE(session_id, request_key)
            );
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              task_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
              notify INTEGER NOT NULL DEFAULT 0, spoken INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capability_aliases(
              task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,call_id TEXT NOT NULL,
              request TEXT NOT NULL,result_id TEXT NOT NULL,
              PRIMARY KEY(task_id,task_revision,call_id)
            );
            CREATE TABLE IF NOT EXISTS task_changes(
              task_id TEXT NOT NULL, change_id TEXT NOT NULL, revision INTEGER NOT NULL,
              previous_revision INTEGER NOT NULL, original_goal TEXT NOT NULL, text TEXT NOT NULL,
              goal TEXT NOT NULL, constraints TEXT NOT NULL, status TEXT NOT NULL,
              run_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
              PRIMARY KEY(task_id,change_id), UNIQUE(task_id,revision)
            );
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
            CREATE TABLE IF NOT EXISTS capability_catalogs(revision TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS capability_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS capability_calls(
              id TEXT PRIMARY KEY,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,call_id TEXT NOT NULL,
              tool_id TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,
              UNIQUE(task_id,task_revision,call_id)
            );
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            for column in ["catalog_revision", "profile_id", "outcome", "final_result", "discovery_mode", "completion_mode", "parent_task_id"]:
                if column not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_session(self):
        sid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?)", (sid, time.time()))
        return sid

    def has_session(self, sid):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is not None

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("找不到任务")
        task = dict(row)
        for field in ("calls", "history"):
            task[field] = json.loads(task[field])
        return task

    def task(self, tid, sid=None):
        with self.connect() as db:
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        if sid is not None and task["session_id"] != sid:
            raise KeyError("找不到任务")
        return task

    def tasks(self, sid):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY created DESC LIMIT 100", (sid,))]

    def _event(self, db, task, kind, data, notify=False):
        db.execute("INSERT INTO events(session_id,task_id,kind,data,notify,created) VALUES(?,?,?,?,?,?)", (task["session_id"], task["id"], kind, json.dumps({"task_revision": task["revision"], **data}, ensure_ascii=False), int(notify), time.time()))

    def create_task(self, sid, goal, request_key, source="hermes", catalog_revision=None, profile_id=None, discovery_mode="off", completion_mode="agent"):
        now = time.time()
        tid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM tasks WHERE session_id=? AND request_key=?", (sid, request_key)).fetchone()
            if old:
                task = self.decode(old)
                first = db.execute('SELECT original_goal FROM task_changes WHERE task_id=? ORDER BY revision LIMIT 1',(task['id'],)).fetchone()
                original_goal = first[0] if first else task['goal']
                if original_goal != goal or task["source"] != source or (profile_id is not None and task.get("profile_id") != profile_id) or (task.get("discovery_mode") or "off") != discovery_mode or (task.get("completion_mode") or "agent") != completion_mode:
                    raise ValueError("请求标识已用于其他内容")
                return task, False
            db.execute("INSERT INTO tasks(id,session_id,request_key,goal,status,source,created,updated,catalog_revision,profile_id,discovery_mode,completion_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (tid, sid, request_key, goal, "running", source, now, now, catalog_revision, profile_id, discovery_mode, completion_mode))
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            self._event(db, task, "task.created", {"message": "已创建生成任务", "source": source})
        return task, True

    def event(self, tid, kind, data, notify=False):
        with self.connect() as db:
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            self._event(db, task, kind, data, notify)

    def events(self, sid, after=0, pending=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE session_id=? AND id>?" + (" AND notify=1 AND spoken=0" if pending else "") + " ORDER BY id LIMIT 200", (sid, after))
            return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def acknowledge(self, sid, event_id):
        with self.connect() as db:
            db.execute("UPDATE events SET spoken=1 WHERE id=? AND session_id=?", (event_id, sid))

    def record_call(self, tid, name, arguments, revision=0):
        """Persist validated previews only. This module has no vehicle transport."""
        call = {"tool": name, "arguments": arguments}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            if task["status"] != "running" or task["revision"] != revision:
                raise ValueError("任务不在执行中，不再接收新的调用结构")
            calls = task["calls"]
            if call not in calls:
                if len(calls) >= 8:
                    raise ValueError("本演示单个任务最多生成 8 个工具调用")
                calls.append(call)
                db.execute("UPDATE tasks SET calls=?,updated=? WHERE id=?", (json.dumps(calls, ensure_ascii=False), time.time(), tid))
                self._event(db, task, "tool.preview", {"call": call, "validated": True, "executed": False})
        return {"status": "preview_generated", "validated": True, "executed": False, "mode": "dry_run", "call": call}

    def ask(self, tid, question, revision=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            if task["status"] != "running" or task["revision"] != revision:
                raise ValueError("任务当前不能追加追问")
            db.execute("UPDATE tasks SET status='awaiting_input',question=?,updated=? WHERE id=?", (question, time.time(), tid))
            self._event(db, task, "task.question", {"message": question, "task_revision": revision}, True)
        return {"status": "awaiting_input", "question": question, "instruction": "本轮结束，等待前台用户补充"}

    def resume(self, tid, sid, answer, expected_revision=None):
        expired = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=? AND session_id=?", (tid, sid)).fetchone())
            from .conversation_routing import expire_in_transaction
            expired = expire_in_transaction(self,db,task)
            if not expired:
                if task["status"] != "awaiting_input":
                    raise ValueError("该任务当前没有等待回答的问题")
                if expected_revision is not None and task["revision"] != expected_revision:
                    raise ValueError("追问已更新，请根据当前问题重新回答")
                history = [*task["history"], {"question": task["question"], "answer": answer}]
                db.execute("UPDATE tasks SET status='running',question=NULL,history=?,revision=revision+1,updated=? WHERE id=?", (json.dumps(history, ensure_ascii=False), time.time(), tid))
                db.execute("UPDATE events SET spoken=1 WHERE task_id=? AND kind='task.question'", (tid,))
                self._event(db, task, "task.resumed", {"message": "已收到补充信息，继续原任务", "task_revision": task["revision"]+1})
        if expired:
            raise ValueError('等待补充已超过五分钟，任务已过期，请新建任务')
        return self.task(tid)

    def set_run(self, tid, run_id):
        with self.connect() as db:
            db.execute("UPDATE tasks SET run_id=?,updated=? WHERE id=?", (run_id, time.time(), tid))

    def finish(self, tid, status, summary, expected_revision=None, expected_run=None):
        if status not in TERMINAL:
            raise ValueError("非法任务终态")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            if expected_revision is not None and task['revision'] != expected_revision:
                return False
            if expected_run is not None and task['run_id'] != expected_run:
                return False
            if task["status"] in TERMINAL or task["status"] in {"awaiting_input","waiting"}:
                return False
            db.execute("UPDATE tasks SET status=?,summary=?,updated=? WHERE id=?", (status, summary, time.time(), tid))
            self._event(db, task, f"task.{status}", {"message": summary, "status": status, "executed": False}, True)
        return True

    def cancel(self, tid, sid, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=? AND session_id=?", (tid, sid)).fetchone())
            if expected_revision is not None and task['revision'] != expected_revision:
                raise ValueError('任务轮次已改变，请确认当前任务')
            if task["status"] in TERMINAL:
                return task
            summary = "生成任务已取消，没有执行车辆控制。"
            db.execute("UPDATE tasks SET status='cancelled',question=NULL,summary=?,updated=? WHERE id=?", (summary, time.time(), tid))
            db.execute("UPDATE events SET notify=0 WHERE task_id=? AND notify=1", (tid,))
            self._event(db, task, "task.cancelled", {"message": summary, "status": "cancelled", "executed": False}, True)
        return self.task(tid)

    def interrupt_running(self):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status='running'").fetchall()
        for row in rows:
            self.finish(row["id"], "interrupted", "服务发生重启，生成任务已中断；没有自动重发。")

    def publish_catalog(self, revision, payload):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO capability_catalogs VALUES(?,?,?)", (revision, json.dumps(payload, ensure_ascii=False), time.time()))
            db.execute("INSERT INTO capability_settings VALUES('active_catalog',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (revision,))

    def active_catalog_revision(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM capability_settings WHERE key='active_catalog'").fetchone()
            return row[0] if row else None

    def session_profile(self, sid):
        with self.connect() as db:
            row = db.execute("SELECT value FROM capability_settings WHERE key=?", ("session_profile:"+sid,)).fetchone()
            return row[0] if row else None

    def select_profile(self, sid, profile_id):
        with self.connect() as db:
            db.execute("INSERT INTO capability_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("session_profile:"+sid, profile_id))

    def catalog_payload(self, revision):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM capability_catalogs WHERE revision=?", (revision,)).fetchone()
            if not row:
                raise KeyError("目录快照不存在")
            return json.loads(row[0])

    def capability_results(self, tid):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT response FROM capability_calls WHERE task_id=? ORDER BY created,rowid", (tid,))]

    def task_navigation(self, tid):
        with self.connect() as db:
            rows = db.execute("SELECT kind,data FROM events WHERE task_id=? AND kind IN ('catalog.browsed','catalog.described','catalog.searched','catalog.preloaded','catalog.preload_skipped','catalog.fallback') ORDER BY id LIMIT 100", (tid,))
            return [{**json.loads(r[1]), "kind": r[0]} for r in rows]

    def task_events(self, tid):
        with self.connect() as db:
            return [{**dict(r), "data": json.loads(r["data"])} for r in db.execute("SELECT id,kind,data,created FROM events WHERE task_id=? ORDER BY id LIMIT 500", (tid,))]

    def event_cursor(self, sid):
        with self.connect() as db:
            return db.execute("SELECT coalesce(max(id),0) FROM events WHERE session_id=?", (sid,)).fetchone()[0]

    def _preload_fallback(self, db, task, reason):
        row = db.execute("SELECT data FROM events WHERE task_id=? AND kind='catalog.preloaded' ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
        if not row or json.loads(row[0]).get("task_revision") != task["revision"]:
            return
        earlier = db.execute("SELECT data FROM events WHERE task_id=? AND kind='catalog.fallback' ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
        if earlier and json.loads(earlier[0]).get("task_revision") == task["revision"]:
            return
        self._event(db, task, "catalog.fallback", {"reason": reason, "task_revision": task["revision"], "message": "继续完整目录与查证流程"})

    def preload_fallback(self, tid, reason):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            self._preload_fallback(db, task, reason)


    def latest_change(self, tid, db=None):
        if db is None:
            with self.connect() as connection:
                return self.latest_change(tid, connection)
        row = db.execute("SELECT * FROM task_changes WHERE task_id=? ORDER BY revision DESC LIMIT 1", (tid,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['constraints'] = json.loads(result['constraints'])
        return result

    def change_by_id(self, tid, change_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM task_changes WHERE task_id=? AND change_id=?", (tid, change_id)).fetchone()
            return dict(row) if row else None

    def revise_goal(self, tid, sid, text, expected_revision, change_id, goal, constraints, question=None):
        result, created = self._revise_goal(tid,sid,text,expected_revision,change_id,goal,constraints,question)
        if result['status']=='expired':
            raise ValueError('等待补充已超过五分钟，任务已过期，请新建任务')
        return result, created

    def _revise_goal(self, tid, sid, text, expected_revision, change_id, goal, constraints, question=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.decode(db.execute('SELECT * FROM tasks WHERE id=? AND session_id=?', (tid, sid)).fetchone())
            old = db.execute('SELECT * FROM task_changes WHERE task_id=? AND change_id=?', (tid, change_id)).fetchone()
            if old:
                if old['text'] != text or old['previous_revision'] != expected_revision:
                    raise ValueError('修改标识已用于其他内容')
                return task, False
            from .conversation_routing import expire_in_transaction
            expired = expire_in_transaction(self,db,task)
            if expired:
                return expired, False
            if task['status'] not in {'running', 'awaiting_input'}:
                raise ValueError('任务已结束，请新建任务；不会重新打开旧任务')
            if task['revision'] != expected_revision:
                raise ValueError('任务已更新，请查看当前目标后再修改')
            now = time.time(); revision = task['revision'] + 1
            db.execute('INSERT INTO task_changes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (tid, change_id, revision, task['revision'], task['goal'], text, goal,
                        json.dumps(constraints, ensure_ascii=False), 'needs_input' if question else 'pending', task['run_id'], now, now))
            db.execute("UPDATE tasks SET goal=?,revision=?,status=?,question=?,calls='[]',summary=NULL,outcome=NULL,final_result=NULL,updated=? WHERE id=?",
                       (goal, revision, 'awaiting_input' if question else 'running', question, now, tid))
            # Old notices remain in history but cease to be eligible for delivery.
            db.execute('UPDATE events SET notify=0 WHERE task_id=? AND notify=1', (tid,))
            task = self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
            self._event(db, task, 'task.goal_revised', {'message':'已记录改口，旧参数和旧通知已失效', 'goal':goal, 'change_id':change_id})
            if question:
                self._event(db, task, 'task.question', {'message':question}, True)
            return task, True

    def change_status(self, tid, revision, status):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
            if task['revision'] != revision or task['status'] in TERMINAL:
                return
            db.execute('UPDATE task_changes SET status=?,updated=? WHERE task_id=? AND revision=?', (status,time.time(),tid,revision))
            self._event(db, task, 'task.steer_'+status, {'message':{'accepted':'Hermes 已接受改口，等待新参数', 'unconfirmed':'改口送达未确认，旧目标仍被拦截', 'rejected':'Hermes 未接受改口，已停止旧目标', 'started':'新目标已随任务开始发送'}.get(status,status)})

    def accepts_run_revision(self, tid, initial_revision, run_id):
        with self.connect() as db:
            task = self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
            if task['run_id'] != run_id:
                return False
            current = initial_revision
            rows = db.execute('SELECT * FROM task_changes WHERE task_id=? AND revision>? ORDER BY revision', (tid, initial_revision))
            for row in rows:
                if row['previous_revision'] != current or row['run_id'] not in (None, run_id):
                    return False
                current = row['revision']
            return current == task['revision']

    def notification_current(self, sid, event_id):
        with self.connect() as db:
            row = db.execute('SELECT e.data,e.notify,e.spoken,t.revision FROM events e JOIN tasks t ON t.id=e.task_id WHERE e.id=? AND e.session_id=?', (event_id,sid)).fetchone()
            return bool(row and row['notify'] and not row['spoken'] and json.loads(row['data']).get('task_revision',0) == row['revision'])
