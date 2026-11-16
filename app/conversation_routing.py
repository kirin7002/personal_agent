"""Deterministic session task routing and transaction-local question expiry.

Only explicit user interactions should call ``focus`` or ``clear``. Routing is
advisory: callers must validate the returned revision under their write lock.
After ``expire_in_transaction`` returns a task, commit before raising a late
answer error, otherwise the caller's transaction would roll back the expiry.
"""
import json
import math
import re
import time


ACTIVE = frozenset({"running", "awaiting_input", "waiting"})
_PUNCTUATION = re.compile(r"[\s，。！？、,!.?；;：:]+")
_CANCEL = frozenset({
    "算了", "不用了", "取消", "取消吧", "取消了", "取消这个", "取消这个任务",
    "取消当前任务", "取消当前这个任务", "取消任务", "把这个任务取消", "这个任务取消",
    "这个任务不用了", "这个任务算了", "停止这个任务", "不用做了", "不需要了",
})
_NEGATED_CANCEL = re.compile(r"(?:别|不要|不用|不想|不能|不许|不是说|不是要|不是|不|并非要|没有说)(?:再|去|把这个任务)?取消")
_HISTORY = re.compile(r"之前|刚才|先前|前面|上一个|那个.*任务")
_EDIT = re.compile(r"(?:改成|改为|改到|换成|调整为|调整到)")
_OBJECTS = {
    "window": ("车窗", "窗户", "窗玻璃"),
    "sunroof": ("天窗",),
    "climate": ("空调", "温度", "制冷", "制热"),
    "seat": ("座椅", "座位"),
    "door": ("车门", "门锁"),
    "light": ("车灯", "大灯", "阅读灯", "氛围灯", "近光灯", "远光灯", "灯光", "照明"),
    "navigation": ("导航", "路线"),
    "music": ("音乐", "歌曲", "播放", "媒体", "音量", "音源"),
    "battery": ("电量", "续航", "充电"),
    "wiper": ("雨刮", "雨刷"),
    "mirror": ("后视镜",),
    "suspension": ("悬架", "悬挂"),
    "defrost": ("除霜", "挡风玻璃"),
    "vehicle": ("车况", "车书", "车辆配置", "本车配置", "驾驶状态"),
}
_ZONES = ("主驾", "副驾", "后排", "左前", "右前", "左后", "右后")
_SHORT_ANSWER = re.compile(
    r"(?:(?:不用|不要)?(?:全部|所有|全车|主驾|副驾|后排|前排|左前|右前|左后|右后)"
    r"(?:分区|区域|车窗|座椅)?(?:就好|就行|吧|的)?)+|"
    r"(?:是|不是|对|不对|好的?|可以|不可以|确认|确定|都要|都不要)|"
    r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十]+)(?:度|档|挡|%|分钟)?"
)
_PARAMETER_ANSWER = re.compile(
    r"(?:四个|两个|所有|全部|主驾|副驾|前排|后排|座位|位置|和|也|都|全|"
    r"开到|调到|设为|开|最高档?|最低档?|一档|二档|三档|就好|就行)+"
)
_CHAT = re.compile(r"^(?:谢谢.*|多谢.*|你好.*|您好.*|再见.*|晚安.*|早上好.*|哈哈.*|嗯|哦|知道了|明白了|今天天气.*|你是谁|你叫什么.*|讲个笑话.*)$")


def _compact(text):
    return _PUNCTUATION.sub("", text)


def is_cancellation(text):
    """Only whole cancellation utterances (including repetitions) are commands."""
    compact = _compact(text)
    if not compact or _NEGATED_CANCEL.search(compact):
        return False
    phrases = "|".join(re.escape(p) for p in sorted(_CANCEL, key=len, reverse=True))
    return re.fullmatch("(?:" + phrases + ")+", compact) is not None


def _clock(now, timeout):
    now = time.time() if now is None else float(now)
    timeout = float(timeout)
    if not math.isfinite(now) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("now and timeout must be finite; timeout must be nonnegative")
    return now, timeout


def expire_in_transaction(store, db, task, now=None, timeout=300):
    """Return the newly expired task or None, inside the caller's write lock.

    Does not begin, commit, or roll back a transaction. Re-read the row and use
    revision/status CAS so a stale snapshot cannot expire a replacement question.
    A question's first delivery in its revision starts its deadline; repeated
    notifications do not extend it. Legacy rows without a matching question
    event use ``updated``. Remote stop/audio invalidation belongs to the caller.
    """
    now, timeout = _clock(now, timeout)
    row = db.execute("SELECT * FROM tasks WHERE id=? AND session_id=?",
                     (task["id"], task["session_id"])).fetchone()
    if row is None or row["status"] != "awaiting_input" or row["revision"] != task["revision"]:
        return None
    current = store.decode(row)
    question_created = current["updated"]
    for event in db.execute(
        "SELECT data,created FROM events WHERE task_id=? AND kind='task.question' ORDER BY id",
        (current["id"],),
    ):
        data = json.loads(event["data"])
        if (data.get("task_revision", 0) == current["revision"]
                and data.get("message", current["question"]) == current["question"]):
            question_created = event["created"]
            break
    if now < question_created + timeout:
        return None
    summary = "等待补充信息超过期限，任务已过期；如需继续，请新建任务。"
    changed = db.execute(
        "UPDATE tasks SET status='expired',revision=revision+1,question=NULL,summary=?,updated=? "
        "WHERE id=? AND session_id=? AND status='awaiting_input' AND revision=?",
        (summary, now, current["id"], current["session_id"], current["revision"]),
    )
    if not changed.rowcount:
        return None
    db.execute("UPDATE events SET notify=0 WHERE task_id=? AND notify=1", (current["id"],))
    expired = store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (current["id"],)).fetchone())
    store._event(db, expired, "task.expired", {
        "message": summary, "status": "expired", "reason": "awaiting_input_timeout",
        "executed": False, "question_created": question_created,
        "deadline": question_created + timeout,
    }, notify=False)
    # Keep the focus row: the terminal task is a tombstone, never a reason to
    # fall back to another older task. current() excludes all terminal statuses.
    return expired


class ConversationRouter:
    def __init__(self, store, catalog=None):
        self.store, self.catalog = store, catalog
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS session_task_focus(
                sid TEXT PRIMARY KEY, task_id TEXT,
                user_event_sequence INTEGER NOT NULL DEFAULT 0
            )""")

    def focus(self, sid, tid):
        """Record an explicit user interaction, after checking task ownership."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.store.decode(db.execute(
                "SELECT * FROM tasks WHERE id=? AND session_id=?", (tid, sid),
            ).fetchone())
            self._write_focus(db, sid, tid)
        return task

    def clear(self, sid):
        """Record explicit focus clearing; retain a NULL tombstone."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise KeyError("找不到会话")
            self._write_focus(db, sid, None)

    @staticmethod
    def _write_focus(db, sid, tid):
        db.execute(
            "INSERT INTO session_task_focus(sid,task_id,user_event_sequence) VALUES(?,?,1) "
            "ON CONFLICT(sid) DO UPDATE SET task_id=excluded.task_id,"
            "user_event_sequence=session_task_focus.user_event_sequence+1", (sid, tid),
        )

    def _focused(self, sid):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise KeyError("找不到会话")
            focus = db.execute("SELECT task_id FROM session_task_focus WHERE sid=?", (sid,)).fetchone()
            if focus is None:
                # Bootstrap once, including terminal tasks. updated is not user
                # interaction order. A NULL row also prevents later bootstraps.
                latest = db.execute(
                    "SELECT id FROM tasks WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                tid = latest["id"] if latest else None
                db.execute("INSERT INTO session_task_focus VALUES(?,?,0)", (sid, tid))
            else:
                tid = focus["task_id"]
            row = db.execute("SELECT * FROM tasks WHERE id=? AND session_id=?", (tid, sid)).fetchone()
            return self.store.decode(row) if row else None

    def current(self, sid):
        """Return the focused active task, never an older active alternative."""
        task = self._focused(sid)
        return task if task and task["status"] in ACTIVE else None

    def expire_waiting(self, timeout=300, now=None):
        """Atomically expire overdue questions across sessions; return changed tasks."""
        now, timeout = _clock(now, timeout)
        expired = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM tasks WHERE status='awaiting_input'").fetchall()
            for row in rows:
                task = expire_in_transaction(self.store, db, row, now=now, timeout=timeout)
                if task is not None:
                    expired.append(task)
        return expired

    @staticmethod
    def _result(kind, text, task=None, explicit=False, **extra):
        return {"kind": kind, "task_id": task["id"] if task else None,
                "task_revision": task["revision"] if task else None,
                "text": text, "explicit": explicit, **extra}

    @staticmethod
    def _candidates(tasks):
        return [{"task_id": t["id"], "task_revision": t["revision"],
                 "goal": t["goal"], "status": t["status"]} for t in tasks]

    @staticmethod
    def _history_parts(text):
        """Separate an explicit reference from its action without substring cancels."""
        parts = re.split(r"[，,。；;：:]", text, maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1].strip()
        # Also accept punctuation-free '之前开车窗那个不用了'. Only a
        # complete suffix is recognized; partial corrections stay intact.
        for action in sorted(_CANCEL, key=len, reverse=True):
            match = re.search(re.escape(action) + r"[。！!\s]*$", text)
            if match:
                return text[:match.start()], action
        match = _EDIT.search(text)
        if match:
            return text[:match.start()], text[match.start():].strip()
        return text, ""

    def _history_matches(self, sid, reference):
        objects = {key for key, names in _OBJECTS.items() if any(n in reference for n in names)}
        zones = {zone for zone in _ZONES if zone in reference}
        opening = bool(re.search(r"打开|开启|开(?:车窗|窗户|天窗|空调|车门)", reference))
        closing = bool(re.search(r"关闭|关上|关(?:车窗|窗户|天窗|空调|车门)", reference))
        matches = []
        with self.store.connect() as db:
            # Do not limit to Store.tasks' newest 100 rows: explicit history
            # references can legitimately address older tasks.
            tasks = [self.store.decode(row) for row in db.execute(
                "SELECT * FROM tasks WHERE session_id=? ORDER BY created DESC,rowid DESC", (sid,),
            )]
        exact = [t for t in tasks if t['goal'] and t['goal'] in reference]
        if exact:
            return exact
        for task in tasks:
            label = " ".join(str(task.get(k) or "") for k in ("goal", "name", "title", "labels"))
            named = any(task.get(k) and str(task[k]) in reference for k in ("name", "title"))
            exact_goal = bool(task["goal"] and task["goal"] in reference)
            if not objects and not named and not exact_goal:
                continue
            if any(not any(name in label for name in _OBJECTS[key]) for key in objects):
                continue
            if any(zone not in label for zone in zones):
                continue
            if opening and not re.search(r"打开|开启|开(?:车窗|窗户|天窗|空调|车门)", label):
                continue
            if closing and not re.search(r"关闭|关上|关(?:车窗|窗户|天窗|空调|车门)", label):
                continue
            matches.append(task)
        return matches

    def _new_request(self, text):
        # Reuse imported capability hints at the conversation boundary as well
        # as in preload. Otherwise a recognized control can be lost as chat
        # before it ever reaches the agent (e.g. 窗户降到百分之五十).
        if self.catalog is not None:
            from .control_hints import recall_matches
            if any(tool['enabled'] and tool['kind'] == 'control'
                   and recall_matches(tool, text) is True
                   for tool in self.catalog.snapshot().tools.values()):
                return True
        return (any(name in text for names in _OBJECTS.values() for name in names)
                and bool(re.search(r"打开|关闭|开启|关上|开|关|调|设|查|多少|怎么|如何|导航|播放|充电|"
                                   r"切换|改为|改成|停止|启用|移动|保存|恢复|折叠|升高|降低|翘起|暂停", text)))

    def resolve(self, sid, text, explicit_task_id=None):
        """Classify a user utterance without executing actions or changing focus.

        ``explicit`` indicates a UI selection or a natural-language historical
        reference. ``text`` strips that reference when it has an action suffix.
        Ownership errors deliberately use the Store's existing KeyError contract.
        Expiry is enforced by the scheduler and transaction-local Store guards.
        """
        text = text.strip()
        explicit = explicit_task_id is not None
        task = self.store.task(explicit_task_id, sid) if explicit else None
        if not self.store.has_session(sid):
            raise KeyError("找不到会话")
        # Check before splitting a historical reference; a suffix '取消' inside
        # '之前那个任务别取消' must never turn into a positive cancellation.
        if _NEGATED_CANCEL.search(_compact(text)):
            return self._result("chat", text, explicit=explicit)
        historical = bool(_HISTORY.search(text))
        if historical:
            reference, action = self._history_parts(text)
            matches = self._history_matches(sid, reference)
            # The user may select one of the candidates we just offered. That
            # explicit choice resolves ambiguity, but cannot override a conflict.
            if task and any(candidate['id']==task['id'] for candidate in matches):
                matches = [task]
            if len(matches) != 1:
                return self._result("clarify", text, explicit=True,
                                    message="请明确要操作的历史任务。", candidates=self._candidates(matches))
            if task and task["id"] != matches[0]["id"]:
                return self._result("clarify", text, explicit=True,
                                    message="页面选择与文字指代的任务不一致，请确认。",
                                    candidates=self._candidates([task, matches[0]]))
            task, explicit = matches[0], True
            text = action or text
        if task is None:
            task = self._focused(sid)
        compact = _compact(text)
        if not compact or _NEGATED_CANCEL.search(compact) or _CHAT.fullmatch(compact):
            return self._result("chat", text, explicit=explicit)
        if explicit and task and task["status"] not in ACTIVE:
            return self._result("historical", text, task, explicit=True,
                                message="原任务已结束，不能恢复或修改；如需办理，请新建任务。")
        active = task if task and task["status"] in ACTIVE else None
        if is_cancellation(text):
            if active:
                return self._result("cancel", text, active, explicit)
            return self._result("clarify", text, explicit=explicit,
                                message="当前没有可取消的任务，请明确任务。")
        if _SHORT_ANSWER.fullmatch(compact) or _PARAMETER_ANSWER.fullmatch(compact):
            if active and active["status"] == "awaiting_input":
                return self._result("answer_question", text, active, explicit)
            message = ("原任务已过期，请说明完整的新需求。" if task and task["status"] == "expired"
                       else "当前没有适用的待答问题，请明确任务或说明完整需求。")
            return self._result("clarify", text, explicit=explicit, message=message)
        if (self._new_request(compact) and not explicit
                and not re.match(r"^(?:请|帮我|还是|现在)*" + _EDIT.pattern, compact)):
            return self._result("new_request", text)
        if _EDIT.search(compact) and not re.search(r"不要|不用|别|不想|不改", compact):
            if active:
                return self._result("revise_goal", text, active, explicit)
            return self._result("clarify", text, explicit=explicit,
                                message="当前没有可修改的任务，请说明完整的新需求。")
        if historical and not action:
            return self._result("historical", text, task, explicit=True)
        if self._new_request(compact):
            return self._result("new_request", text, explicit=explicit)
        # Answers can be longer than a slot token (e.g. four seats at maximum).
        # Full vehicle requests, chat, cancellations and edits were handled above.
        if active and active["status"] == "awaiting_input":
            return self._result("answer_question", text, active, explicit)
        return self._result("chat", text, explicit=explicit)
