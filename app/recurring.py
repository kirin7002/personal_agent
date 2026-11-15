"""Local periodic plans; persistence only, with no scheduler or remote calls.

create returns (plan, created); update returns {plan, stopped_task_ids}; due
returns {tasks, stopped_task_ids}. The parent stops returned task IDs and checks
each returned task's current status/revision before launch. run_id is preserved
on cancellation for remote stop. This is not a crash/restart delivery protocol.

Schedules use frequency=daily/weekly and time=HH:MM (weekly weekdays: Monday=0).
frequency=interval, seconds>=60 is also supported. Dates are calculated in the
stored IANA timezone: nonexistent wall times are skipped and ambiguous wall
times use their first occurrence only. All persisted timestamps are UTC epochs.

Plan events have a separate integer cursor/ID namespace. Adapters should use
"plan:<id>" and dispatch current/ack calls here, not to Store. Normal execution
results use the existing task outbox. Missed/overlapping occurrences each emit
one plan notification; acknowledgements are explicit and session scoped.
"""
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.store import Store


GRACE_SECONDS = 60


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _epoch(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone aware")
        value = value.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("now must be a finite nonnegative epoch or aware datetime")
    return float(value)


def _zone(name):
    _text(name, "timezone")
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be an installed IANA timezone") from exc
    return name


def local_timezone_name():
    """Resolve runtime local IANA rules, never infer a zone from a UTC offset.

    TZ takes precedence, including :/path/to/zoneinfo/...; Linux /etc/timezone
    and Linux/macOS /etc/localtime symlinks are supported. A copied tzfile with
    no IANA identity requires explicit configuration instead of guessing.
    """
    configured = os.environ.get("TZ")
    if configured:
        name = configured.removeprefix(":")
        if "/zoneinfo/" in name:
            name = name.split("/zoneinfo/", 1)[1]
        return _zone(name)
    local = Path("/etc/localtime").resolve()
    if "/zoneinfo/" in str(local):
        return _zone(str(local).split("/zoneinfo/", 1)[1])
    try:
        name = Path("/etc/timezone").read_text().strip()
    except OSError:
        name = ""
    if name:
        return _zone(name)
    key = getattr(datetime.now().astimezone().tzinfo, "key", None)
    if key:
        return _zone(key)
    raise ValueError("Cannot identify local IANA timezone; set TZ or pass timezone")


def normalize_schedule(schedule, *, timezone=None):
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    data = dict(schedule)
    # Accept the initial proposed spelling; public records always use frequency.
    if "kind" in data:
        kind = data.pop("kind")
        if "frequency" in data and data["frequency"] != kind:
            raise ValueError("Conflicting schedule frequency")
        data["frequency"] = kind
    frequency = data.get("frequency")
    if frequency not in ("daily", "weekly", "interval"):
        raise ValueError("frequency must be daily, weekly or interval")
    allowed = {"frequency", "timezone", "seconds"} if frequency == "interval" else {
        "frequency", "timezone", "time", "weekdays"}
    if data.keys() - allowed:
        raise ValueError("Unknown schedule fields")
    zone = _zone(data["timezone"] if "timezone" in data else timezone or local_timezone_name())
    if frequency == "interval":
        seconds = data.get("seconds")
        if type(seconds) is not int or seconds < 60:
            raise ValueError("interval seconds must be an integer >=60")
        return {"frequency": frequency, "seconds": seconds, "timezone": zone}
    at = data.get("time")
    if not isinstance(at, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("time must be explicit HH:MM")
    days = data.get("weekdays", [])
    if not isinstance(days, list) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise ValueError("weekdays must be a list of integers Monday=0 through Sunday=6")
    if frequency == "weekly" and not days:
        raise ValueError("weekly schedule requires weekdays")
    if frequency == "daily" and days:
        raise ValueError("daily schedule must not restrict weekdays")
    return {"frequency": frequency, "time": at, "weekdays": sorted(set(days)), "timezone": zone}


def next_occurrence(schedule, after):
    """First occurrence strictly after after; schedule must be normalized.

    Round trips through UTC reject DST gaps. Choosing the earliest valid fold
    before comparing against after prevents a second run during a repeated hour.
    """
    after = _epoch(after)
    if schedule["frequency"] == "interval":
        return after + schedule["seconds"]
    zone = ZoneInfo(schedule["timezone"])
    day = datetime.fromtimestamp(after, zone).date()
    hour, minute = map(int, schedule["time"].split(":"))
    for offset in range(370):
        date = day + timedelta(days=offset)
        if schedule["frequency"] == "weekly" and date.weekday() not in schedule["weekdays"]:
            continue
        wall = datetime(date.year, date.month, date.day, hour, minute)
        candidates = []
        for fold in (0, 1):
            aware = wall.replace(tzinfo=zone, fold=fold)
            stamp = aware.timestamp()
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == wall:
                candidates.append(stamp)
        if candidates and min(candidates) > after:
            return min(candidates)
    raise ValueError("No valid occurrence in the next year")


def _chinese_number(value):
    if value.isdigit():
        return int(value)
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if "十" in value:
        tens, ones = value.split("十", 1)
        return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    return digits[value]


_NUMBER = r"(?:\d{1,3}|[零一二两三四五六七八九十]{1,3})"
_PERIOD = r"凌晨|早上|早晨|上午|中午|下午|晚上|晚间"
_RECURRING_HINT = re.compile(
    r"每(?:天|日|周|星期|月|年|季度|个(?:月|星期|小时)|隔|分钟|小时|秒|"
    r"[0-9零一二两三四五六七八九十]+(?:秒|分钟|小时|天|周|月))")
_REQUEST_PREFIX = re.compile(
    r"^(?:请你|请|麻烦你|麻烦|帮我|帮忙|我想要|我想|我希望|我要|以后|今后|"
    r"从现在起|从今天起|安排一下|安排|设置一个|设置|创建一个|创建|在)\s*")


def _plan_control(text):
    """Leave plan controls to the parent's routing precedence, never create drafts."""
    action = re.search(r"暂停|取消|恢复|停止|删除|继续", text)
    hint = _RECURRING_HINT.search(text)
    return bool(action and ((hint and action.start() < hint.start()) or "计划" in text))


def _days(text):
    match = re.match(r"(?:周|星期)?[一二三四五六日天](?:[、,，和](?:周|星期)?[一二三四五六日天])*", text)
    if not match:
        return [], text
    days = sorted({"一二三四五六日".index(c.replace("天", "日"))
                   for c in match[0] if c in "一二三四五六日天"})
    return days, text[match.end():].lstrip()


def _clock_text(text, period=None):
    prefix = re.match(rf"^(?:的|在)?\s*(?P<period>{_PERIOD})?\s*", text)
    period = prefix["period"] or period
    rest = text[prefix.end():]
    at = re.match(rf"(?P<hour>{_NUMBER})(?:(?:点|时)(?P<minute>半|一刻|三刻|\d{{1,3}}分?|{_NUMBER}分)?"
                  rf"|[:：](?P<colon>\d{{2}})(?!\d))", rest)
    if not at:
        return None, rest, period
    tail = rest[at.end():].lstrip()
    remainder = tail.lstrip(" ，,。:：")
    try:
        hour = _chinese_number(at["hour"])
        minute_text = at["colon"] or at["minute"]
        if minute_text in ("半", "一刻", "三刻"):
            minute = {"半": 30, "一刻": 15, "三刻": 45}[minute_text]
        else:
            minute = _chinese_number(minute_text.removesuffix("分")) if minute_text else 0
        if period in ("下午", "晚上", "晚间") and 1 <= hour < 12:
            hour += 12
        elif period in ("晚上", "晚间", "凌晨") and hour == 12:
            hour = 0
        elif period == "中午" and 1 <= hour <= 2:
            hour += 12
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Invalid time")
        if period in ("凌晨", "早上", "早晨", "上午") and hour > 12:
            raise ValueError("Conflicting day period")
        # Multiple times/ranges require clarification; never silently choose one.
        more = re.match(r"^(?:或者|以及|和|或|到|至|[、,，~～-])\s*", tail)
        if more and re.match(rf"(?:{_PERIOD})?\s*(?:\d|[一二三四五六七八九十])", tail[more.end():]):
            # Consume the remaining clock wording only to preserve the goal;
            # none of these times is selected for execution without clarification.
            _, goal, _ = _clock_text(tail[more.end():])
            return None, goal, period
        if tail.startswith((":", "：", "秒")):
            return None, remainder, period
        return f"{hour:02d}:{minute:02d}", remainder, period
    except (KeyError, ValueError):
        return None, remainder, period


def _parsed(goal, schedule, *, period=None, original_text=None, missing=None):
    missing = list(missing or [])
    frequency = schedule.get("frequency")
    if frequency not in {"daily", "weekly", "interval"}:
        missing.append("frequency")
    elif frequency == "interval":
        if "seconds" not in schedule:
            missing.append("seconds")
    else:
        if frequency == "weekly" and not schedule.get("weekdays"):
            missing.append("weekdays")
        if "time" not in schedule:
            missing.append("time")
    if not goal.strip():
        missing.append("goal")
    missing = list(dict.fromkeys(missing))
    labels = {"frequency": "支持的周期（每天、每周或至少60秒的间隔）", "seconds": "不少于60秒的间隔",
              "weekdays": "每周哪几天", "time": "一个明确的执行时间（几点几分）", "goal": "要执行的任务"}
    question = "请补充" + "、".join(labels[key] for key in missing) + "。" if missing else None
    return {"status": "needs_info" if missing else "ready", "goal": goal.strip(), "schedule": schedule,
            "missing": missing, "question": question, "day_period": period, "original_text": original_text}


def parse_schedule_request(text, *, timezone=None, draft=None):
    """Parse a bounded Chinese schedule, returning ready/needs_info/not_recurring.

    Unsupported recurring prefixes (e.g. 每月) require clarification rather than
    falling through into a one-off vehicle task. A draft can be completed later.
    """
    if draft is not None:
        return complete_schedule_request(draft, text)
    text = _text(text, "text")
    rest = text
    while prefix := _REQUEST_PREFIX.match(rest):
        rest = rest[prefix.end():].lstrip()
    hint = _RECURRING_HINT.search(text)
    if _plan_control(text) or (not rest.startswith("每") and not hint):
        return {"status": "not_recurring", "goal": text, "schedule": None,
                "missing": [], "question": None}
    zone = _zone(timezone or local_timezone_name())
    if not rest.startswith("每"):
        # Keep the original wording for clarification, never discard unknown
        # constraints such as a future start date and execute the remaining goal.
        return _parsed(text, {"timezone": zone}, original_text=text)
    interval = re.match(rf"每(?:隔)?(?P<count>{_NUMBER})?(?P<unit>秒钟|秒|分钟|小时)", rest)
    if interval:
        schedule = {"frequency": "interval", "timezone": zone}
        try:
            seconds = (_chinese_number(interval["count"]) if interval["count"] else 1) * {
                "秒": 1, "秒钟": 1, "分钟": 60, "小时": 3600}[interval["unit"]]
            if seconds >= 60:
                schedule["seconds"] = seconds
        except (KeyError, ValueError):
            pass
        return _parsed(rest[interval.end():].lstrip(" ，,"), schedule, original_text=text)
    start = re.match(r"每天|每日|每周|每星期", rest)
    if not start:
        return _parsed(text, {"timezone": zone}, original_text=text)
    weekly = start[0] in {"每周", "每星期"}
    schedule = {"frequency": "weekly" if weekly else "daily", "timezone": zone, "weekdays": []}
    rest = rest[start.end():].lstrip()
    if weekly:
        schedule["weekdays"], rest = _days(rest)
        # Ranges must not become a partial Monday-only schedule.
        if rest.startswith(("到", "至", "-", "~", "～")):
            schedule["weekdays"] = []
    at, rest, period = _clock_text(rest)
    if at:
        schedule["time"] = at
    return _parsed(rest, schedule, period=period, original_text=text)


def complete_schedule_request(parsed, text):
    """Fill missing fields without replacing a known goal/time/day or mutating draft.

    A complete new recurring sentence replaces the schedule; a schedule-only
    reply retains the draft goal. Unsupported frequency drafts require a new
    supported sentence so an unsupported date never gets embedded in the goal.
    """
    text = _text(text, "text")
    if not isinstance(parsed, dict) or parsed.get("status") != "needs_info":
        raise ValueError("A needs_info schedule draft is required")
    schedule = dict(parsed.get("schedule") or {})
    fresh = parse_schedule_request(text, timezone=schedule.get("timezone"))
    if fresh["status"] != "not_recurring":
        if not fresh["goal"] and "frequency" not in parsed["missing"]:
            return _parsed(parsed["goal"], fresh["schedule"], period=fresh.get("day_period"),
                           original_text=parsed.get("original_text"))
        return fresh
    if "frequency" in parsed["missing"]:
        return _parsed(parsed["goal"], schedule, original_text=parsed.get("original_text"))
    rest, period = text, parsed.get("day_period")
    if "seconds" in parsed["missing"]:
        match = re.fullmatch(rf"({_NUMBER})(秒钟|秒|分钟|小时)", rest)
        if match:
            count = _chinese_number(match[1]) * {"秒": 1, "秒钟": 1, "分钟": 60, "小时": 3600}[match[2]]
            if count >= 60:
                schedule["seconds"] = count
                rest = ""
    if "weekdays" in parsed["missing"]:
        days, remainder = _days(rest)
        if days and not remainder.startswith(("到", "至", "-", "~", "～")):
            schedule["weekdays"], rest = days, remainder
    if "time" in parsed["missing"]:
        at, rest, period = _clock_text(rest, period)
        if at:
            schedule["time"] = at
    goal = parsed["goal"] or rest.strip(" ，,。")
    return _parsed(goal, schedule, period=period, original_text=parsed.get("original_text"))


def parse_schedule_text(text, *, timezone=None):
    """Compatibility helper: return None for a non-recurring request."""
    parsed = parse_schedule_request(text, timezone=timezone)
    return None if parsed["status"] == "not_recurring" else parsed


class RecurringPlans:
    def __init__(self, store: Store, settings=None, *, clock=None, timezone=None):
        self.store = store
        self.clock = clock if clock is not None else time.time
        self.timezone = _zone(timezone or getattr(settings, "timezone", None) or local_timezone_name())
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS task_plans(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    request_key TEXT NOT NULL, original_request TEXT NOT NULL,
                    goal TEXT NOT NULL, schedule TEXT NOT NULL, timezone TEXT NOT NULL,
                    profile_id TEXT, status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','paused','cancelled')),
                    revision INTEGER NOT NULL DEFAULT 0, next_run_at REAL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(session_id,request_key)
                );
                CREATE INDEX IF NOT EXISTS task_plans_due ON task_plans(status,next_run_at);
                CREATE TABLE IF NOT EXISTS task_plan_runs(
                    plan_id TEXT NOT NULL, scheduled_at REAL NOT NULL, task_id TEXT UNIQUE,
                    plan_revision INTEGER NOT NULL, status TEXT NOT NULL,
                    reason TEXT, created REAL NOT NULL,
                    PRIMARY KEY(plan_id,scheduled_at)
                );
                CREATE TABLE IF NOT EXISTS plan_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL, plan_revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL,
                    notify INTEGER NOT NULL DEFAULT 0, spoken INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS plan_events_session ON plan_events(session_id,id);
            """)

    def _now(self, now):
        return _epoch(self.clock() if now is None else now)

    @staticmethod
    def _decode(row):
        if row is None:
            raise KeyError("Plan not found")
        plan = dict(row)
        plan["schedule"] = json.loads(plan["schedule"])
        plan.pop("original_request", None)
        return plan

    def _get(self, db, sid, pid):
        return self._decode(db.execute("SELECT * FROM task_plans WHERE id=? AND session_id=?",
                                       (pid, sid)).fetchone())

    def _event(self, db, plan, kind, message, now, *, notify=True, **data):
        payload = {"message": message, "goal": plan["goal"], "plan_revision": plan["revision"], **data}
        db.execute("""INSERT INTO plan_events
            (session_id,plan_id,plan_revision,kind,data,notify,created) VALUES(?,?,?,?,?,?,?)""",
                   (plan["session_id"], plan["id"], plan["revision"], kind, _json(payload), int(notify), now))

    def create(self, sid, request_key, goal, schedule, profile_id=None, *, now=None):
        _text(sid, "sid")
        _text(request_key, "request_key")
        goal = _text(goal, "goal")
        if profile_id is not None:
            _text(profile_id, "profile_id")
        schedule = normalize_schedule(schedule, timezone=self.timezone)
        original = _json({"goal": goal, "schedule": schedule, "profile_id": profile_id})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise KeyError("Session not found")
            old = db.execute("SELECT * FROM task_plans WHERE session_id=? AND request_key=?",
                             (sid, request_key)).fetchone()
            if old:
                if old["original_request"] != original:
                    raise ValueError("Request key conflicts with an earlier plan")
                return self._decode(old), False
            now = self._now(now)
            pid = uuid.uuid4().hex
            db.execute("""INSERT INTO task_plans
                (id,session_id,request_key,original_request,goal,schedule,timezone,profile_id,
                 next_run_at,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (pid, sid, request_key, original, goal, _json(schedule), schedule["timezone"],
                        profile_id, next_occurrence(schedule, now), now, now))
            plan = self._get(db, sid, pid)
            self._event(db, plan, "plan.created", "已创建周期计划", now)
            return plan, True

    def list(self, sid):
        with self.store.connect() as db:
            return [self._decode(row) for row in db.execute(
                "SELECT * FROM task_plans WHERE session_id=? ORDER BY created,id", (sid,))]

    def get(self, sid, pid):
        with self.store.connect() as db:
            return self._get(db, sid, pid)

    def runs(self, sid, pid):
        with self.store.connect() as db:
            self._get(db, sid, pid)
            return [dict(row) for row in db.execute("""SELECT r.*,t.status AS task_status
                FROM task_plan_runs r LEFT JOIN tasks t ON t.id=r.task_id
                WHERE r.plan_id=? ORDER BY r.scheduled_at""", (pid,))]

    @staticmethod
    def _active(db, pid):
        return db.execute("""SELECT t.* FROM tasks t JOIN task_plan_runs r ON r.task_id=t.id
            WHERE r.plan_id=? AND t.status IN ('running','awaiting_input','waiting')
            ORDER BY r.scheduled_at""", (pid,)).fetchall()

    def _stop(self, db, plan, rows, now, reason):
        stopped = []
        has_waits = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='loop_waits'").fetchone()
        for row in rows:
            tid = row["id"]
            db.execute("""UPDATE tasks SET status='cancelled',revision=revision+1,
                question=NULL,summary=?,updated=? WHERE id=?""", (reason, now, tid))
            db.execute("UPDATE events SET notify=0 WHERE task_id=?", (tid,))
            db.execute("UPDATE task_plan_runs SET status='cancelled',reason=? WHERE task_id=?", (reason, tid))
            if has_waits:
                db.execute("UPDATE loop_waits SET status='invalidated',updated=? WHERE task_id=? AND status='pending'",
                           (now, tid))
            task = self.store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            self.store._event(db, task, "task.cancelled", {
                "message": reason, "status": "cancelled", "plan_id": plan["id"], "executed": False,
            }, False)
            stopped.append(tid)
        return stopped

    def update(self, sid, pid, expected_revision, action, *, goal=None, schedule=None,
               profile_id=None, now=None):
        """CAS control; cancel_plan stops current executions just like pause.

        cancel_once stops all active occurrences (normally one), or skips the
        next scheduled occurrence when none is active. Paused plans have none.
        Every edit skips/accounts for unclaimed old due occurrences, then computes
        a strictly future trigger; an existing run retains its own snapshot.
        profile_id=None on edit means unchanged (explicit profiles stay pinned).
        """
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if action not in {"pause", "resume", "cancel_once", "cancel_plan", "edit"}:
            raise ValueError("Explicit action required: pause, resume, cancel_once, cancel_plan or edit")
        if action != "edit" and any(value is not None for value in (goal, schedule, profile_id)):
            raise ValueError("Changes require the edit action")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._get(db, sid, pid)
            if plan["revision"] != expected_revision:
                raise ValueError("Plan revision changed")
            if plan["status"] == "cancelled":
                raise ValueError("Cancelled plan is terminal")
            now = self._now(now)
            status, next_run = plan["status"], plan["next_run_at"]
            new_goal, new_schedule, new_profile = plan["goal"], plan["schedule"], plan["profile_id"]
            active = self._active(db, pid)
            stopped = []
            superseded = []
            if action in {"pause", "cancel_plan"}:
                status = "paused" if action == "pause" else "cancelled"
                next_run = None
                stopped = self._stop(db, plan, active, now, "周期计划已暂停，本次执行已停止" if action == "pause"
                                     else "周期计划已取消，本次执行已停止")
                # Completed history stays intact; stale success notices do not play after stop.
                db.execute("UPDATE events SET notify=0 WHERE task_id IN (SELECT task_id FROM task_plan_runs WHERE plan_id=?)",
                           (pid,))
            elif action == "resume":
                if status != "paused":
                    raise ValueError("Only paused plans can resume")
                status, next_run = "pending", next_occurrence(new_schedule, now)
            elif action == "cancel_once":
                if active:
                    stopped = self._stop(db, plan, active, now, "已取消周期计划的本次执行")
                elif status == "pending" and next_run is not None:
                    # User explicitly skips this occurrence; it is never later claimed.
                    db.execute("""INSERT OR IGNORE INTO task_plan_runs
                        (plan_id,scheduled_at,plan_revision,status,reason,created)
                        VALUES(?,?,?,'cancelled','cancel_once',?)""", (pid, next_run, plan["revision"], now))
                    next_run = next_occurrence(new_schedule, next_run)
                else:
                    raise ValueError("No active or scheduled occurrence to cancel")
            else:
                if all(value is None for value in (goal, schedule, profile_id)):
                    raise ValueError("edit requires a change")
                if goal is not None:
                    new_goal = _text(goal, "goal")
                if profile_id is not None:
                    new_profile = _text(profile_id, "profile_id")
                if schedule is not None:
                    new_schedule = normalize_schedule(schedule, timezone=plan["timezone"])
                if status == "pending":
                    # Even a goal/profile-only edit must not reinterpret an old
                    # trigger using the new revision. Preserve the original
                    # occurrence's revision in history, without creating a task.
                    old_due = plan["next_run_at"]
                    while old_due is not None and old_due <= now:
                        missed = now - old_due > GRACE_SECONDS
                        run_status = "missed" if missed else "skipped"
                        reason = "late_beyond_grace" if missed else "plan_edited_before_dispatch"
                        inserted = db.execute("""INSERT OR IGNORE INTO task_plan_runs
                            (plan_id,scheduled_at,plan_revision,status,reason,created) VALUES(?,?,?,?,?,?)""",
                                              (pid, old_due, plan["revision"], run_status, reason, now)).rowcount
                        if inserted:
                            superseded.append((old_due, run_status, reason, plan["revision"]))
                        old_due = next_occurrence(plan["schedule"], old_due)
                    next_run = next_occurrence(new_schedule, now)
                else:
                    next_run = None
            db.execute("""UPDATE task_plans SET status=?,next_run_at=?,goal=?,schedule=?,timezone=?,
                profile_id=?,revision=revision+1,updated=? WHERE id=?""",
                       (status, next_run, new_goal, _json(new_schedule), new_schedule["timezone"], new_profile, now, pid))
            db.execute("UPDATE plan_events SET notify=0 WHERE plan_id=?", (pid,))
            plan = self._get(db, sid, pid)
            message = {"pause": "已暂停周期计划，并停止当前执行", "resume": "已恢复周期计划，只安排之后的触发",
                       "cancel_plan": "已取消整个周期计划，并停止当前执行", "cancel_once": "已取消这一次，后续周期保留",
                       "edit": "已更新周期计划，修改适用于之后的执行"}[action]
            self._event(db, plan, "plan." + action, message, now, stopped_task_ids=stopped)
            for scheduled_at, run_status, reason, old_revision in superseded:
                message = ("编辑计划时发现已错过旧触发：超过60秒宽限期，未补跑；延迟根因未确认"
                           if run_status == "missed" else "编辑计划时跳过尚未领取的旧触发，未按新目标补跑")
                self._event(db, plan, "plan." + run_status, message, now, reason=reason,
                            scheduled_at=scheduled_at, occurrence_plan_revision=old_revision,
                            lateness_seconds=now-scheduled_at)
            stopped_tasks = [self.store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
                             for tid in stopped]
            return {"plan": plan, "stopped_task_ids": stopped, "stopped_tasks": stopped_tasks}

    def due(self, catalog_revision, default_profile, discovery_mode, completion_mode, now=None):
        """Claim occurrences AND insert tasks/outboxes within one write transaction.

        No catalogue/network lookups occur here. Parent supplies a mock catalogue
        revision and validates selected profiles. Lateness strictly >60s is a
        miss; no execution is created. Every elapsed occurrence is recorded.
        """
        if discovery_mode not in {"auto", "off"} or completion_mode not in {"auto", "agent"}:
            raise ValueError("Invalid discovery_mode or completion_mode")
        tasks = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self._now(now)
            rows = db.execute("SELECT * FROM task_plans WHERE status='pending' AND next_run_at<=? ORDER BY next_run_at,id",
                              (now,)).fetchall()
            for row in rows:
                plan = self._decode(row)
                scheduled = plan["next_run_at"]
                while scheduled <= now:
                    claimed = db.execute("""INSERT OR IGNORE INTO task_plan_runs
                        (plan_id,scheduled_at,plan_revision,status,created) VALUES(?,?,?,'claimed',?)""",
                                         (plan["id"], scheduled, plan["revision"], now)).rowcount
                    if claimed:
                        reason = "late_beyond_grace" if now - scheduled > GRACE_SECONDS else (
                            "previous_occurrence_active" if self._active(db, plan["id"]) else None)
                        if reason:
                            status = "missed" if reason == "late_beyond_grace" else "skipped"
                            db.execute("UPDATE task_plan_runs SET status=?,reason=? WHERE plan_id=? AND scheduled_at=?",
                                       (status, reason, plan["id"], scheduled))
                            message = ("已错过本次触发：调度检查时超过60秒宽限期，未补跑；延迟根因未确认"
                                       if status == "missed" else "已跳过本次触发：上次执行尚未结束（包括等待），未排队")
                            self._event(db, plan, "plan." + status, message, now, reason=reason,
                                        scheduled_at=scheduled, lateness_seconds=now-scheduled)
                        else:
                            tid = uuid.uuid4().hex
                            selected = db.execute("SELECT value FROM capability_settings WHERE key=?",
                                                  ("session_profile:" + plan["session_id"],)).fetchone()
                            profile = plan["profile_id"] or (selected[0] if selected else None) or default_profile
                            db.execute("""INSERT INTO tasks
                                (id,session_id,request_key,goal,status,source,created,updated,
                                 catalog_revision,profile_id,discovery_mode,completion_mode)
                                VALUES(?,?,?,?,'running','hermes',?,?,?,?,?,?)""",
                                       (tid, plan["session_id"], "recurring:" + plan["id"] + ":" + float(scheduled).hex(),
                                        plan["goal"], now, now, catalog_revision, profile, discovery_mode, completion_mode))
                            db.execute("UPDATE task_plan_runs SET status='created',task_id=? WHERE plan_id=? AND scheduled_at=?",
                                       (tid, plan["id"], scheduled))
                            task = self.store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
                            self.store._event(db, task, "task.created", {
                                "message": "周期计划已创建本次任务", "source": "hermes", "plan_id": plan["id"],
                                "plan_revision": plan["revision"], "scheduled_at": scheduled,
                            })
                            self._event(db, plan, "plan.triggered", "已创建本次周期执行", now,
                                        notify=False, task_id=tid, scheduled_at=scheduled)
                            tasks.append(task)
                    scheduled = next_occurrence(plan["schedule"], scheduled)
                db.execute("UPDATE task_plans SET next_run_at=?,updated=? WHERE id=?", (scheduled, now, plan["id"]))
        return {"tasks": tasks, "stopped_task_ids": []}

    def events(self, sid, after=0, pending=False):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM plan_events WHERE session_id=? AND id>?" +
                              (" AND notify=1 AND spoken=0" if pending else "") + " ORDER BY id LIMIT 200", (sid, after))
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def notifications(self, sid, after=0):
        return self.events(sid, after, pending=True)

    def notification_current(self, sid, event_id):
        with self.store.connect() as db:
            row = db.execute("""SELECT e.notify,e.spoken,e.plan_revision,p.revision FROM plan_events e
                JOIN task_plans p ON p.id=e.plan_id WHERE e.id=? AND e.session_id=?""", (event_id, sid)).fetchone()
            return bool(row and row["notify"] and not row["spoken"] and row["plan_revision"] == row["revision"])

    def acknowledge(self, sid, event_id):
        with self.store.connect() as db:
            db.execute("UPDATE plan_events SET spoken=1 WHERE id=? AND session_id=?", (event_id, sid))

    def event_cursor(self, sid):
        with self.store.connect() as db:
            return db.execute("SELECT coalesce(max(id),0) FROM plan_events WHERE session_id=?", (sid,)).fetchone()[0]
