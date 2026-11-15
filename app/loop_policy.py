"""Deterministic no-progress guard, called after CapabilityTools.call has a result.

The parent captures the task before calling the tool and passes that snapshot to
``LoopPolicy(store).observe(task, name, args, result)``. None means continue; a
diagnostic means store.ask paused this revision and the manager should stop the
remote run. The parent owns preview reuse *before* business execution.

Receipt IDs and observation times are not facts. Tool/entity IDs, versions and
business arguments are: never strip arbitrary ``id`` or timestamp parameters.
Backend/mock gaps do not prove bad parameters or missing vehicle equipment.
"""
import hashlib
import json


_VOLATILE = {
    "call_id", "result_id", "request_id", "trace_id", "event_id", "wait_id",
    "timestamp", "observed_at", "sample_observed_at", "created_at", "updated_at",
    "started_at", "finished_at", "elapsed_ms", "duration_ms",
}
_WAITING = {"pending", "waiting", "awaiting_input", "awaiting_result"}
_INACTIVE = {"TASK_INACTIVE", "STALE_REVISION", "TASK_REVISION_MISMATCH"}
_DISCOVERY = {"search_capabilities": "matches", "browse_capabilities": "items",
              "describe_tools": "tools"}


def _meaningful(value):
    if isinstance(value, dict):
        return {k: _meaningful(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_meaningful(v) for v in value]
    return value


def _key(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _classification(error):
    code = error.get("code", "UNKNOWN")
    if code in {"INVALID_ARGUMENTS", "INVALID_REQUEST", "OUTDATED_TARGET"}:
        return "parameters", "请核对参数或最新目标约束；不能擅自改写用户目标。"
    if code in {"NO_MOCK_MATCH", "BACKEND_NOT_CONFIGURED", "INTERFACE_UNAVAILABLE"}:
        return "backend_missing", "缺少模拟数据或后端接口；修改用户参数不能保证修复，也不能据此断言车辆未配备。"
    if code == "INVALID_MOCK_OUTPUT":
        return "backend_error", "后端结果不符合 Schema，需要修复数据或适配器。"
    if code in {"NOT_EQUIPPED", "TOOL_DISABLED", "TOOL_NOT_FOUND"}:
        return "capability_unavailable", "请依据配置和查询证据确认能力范围；目录中找不到工具不等于车辆未配备。"
    if code == "PRECONDITION_NOT_MET":
        return "precondition", "需要新的相关状态事实或用户补充，不能在相同条件下重复尝试。"
    if code in {"TIMEOUT", "SERVICE_UNAVAILABLE", "BACKEND_UNAVAILABLE"}:
        return "transient", "暂时性服务错误已达到重试边界，请稍后重试。"
    if code == "EMPTY_DISCOVERY":
        return "empty_discovery", "重复检索未取得新线索；请补充需求或调整检索范围，零结果不等于不支持。"
    return "tool_error", "请核对错误原因并补充新信息后再继续。"


def _inactive(result):
    error = result.get("error") or {}
    return (error.get("code") in _INACTIVE or
            (error.get("code") == "INVALID_CONTEXT" and
             "TASK_INACTIVE" in error.get("message", "")))


class LoopPolicy:
    def __init__(self, store, limit=3):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 3:
            raise ValueError("no-progress limit must be at least 3")
        self.store, self.limit = store, limit
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS loop_policy_facts(
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY(task_id, revision, fingerprint));
                CREATE TABLE IF NOT EXISTS loop_policy_attempts(
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, attempts INTEGER NOT NULL,
                    PRIMARY KEY(task_id, revision, fingerprint));
            """)

    def observe(self, task, name, args, result):
        """Count repeated failures/empty discovery; pause once, never a newer goal.

        Successful reads only reset counters for previously unseen facts. Pending
        waits neither spend nor reset the budget. Each failed business item has
        its own signature, so changing batch order/call IDs cannot hide a loop.
        """
        if task is None or not isinstance(result, dict):
            return None
        tid, revision = task["id"], task["revision"]
        if (args.get("task_id", tid) != tid or
                args.get("task_revision", revision) != revision or
                result.get("task_revision", revision) != revision or
                result.get("status") in _WAITING or _inactive(result)):
            return None
        facts, failures = [], []
        if name == "call_tools" and not result.get("error"):
            calls = args.get("calls", [])
            by_id = {call.get("call_id"): call for call in calls}
            entries = [(by_id.get(item.get("call_id")) or
                        (calls[i] if i < len(calls) else {}), item)
                       for i, item in enumerate(result.get("results", []))]
        elif name == "describe_tools" and not result.get("error"):
            entries = [({"tool_id": item.get("id")}, item)
                       for item in result.get("tools", [])]
        else:
            entries = [(args, result)]
        for request, item in entries:
            if item.get("status") in _WAITING or _inactive(item):
                continue
            # Business arguments remain exact, including any business IDs/times.
            action = {k: v for k, v in request.items()
                      if k not in _VOLATILE | {"task_id", "task_revision", "complete_if_success"}}
            action = [name, args.get("catalog_revision"), action]
            error = item.get("error")
            if error or item.get("status") in {"error", "failed"}:
                error = error or {"code": "UNKNOWN", "message": "工具调用失败"}
                failures.append((_key([action, _meaningful(item)]), error))
                # Actual precondition values are evidence; error wording is not.
                for detail in error.get("details", []):
                    if isinstance(detail, dict) and "actual" in detail and "pointer" in detail:
                        facts.append(["condition", detail["pointer"], _meaningful(detail["actual"])])
            elif name in _DISCOVERY:
                field = _DISCOVERY[name]
                values = [item] if name == "describe_tools" else item.get(field, [])
                if values:
                    facts.extend(["definition", _meaningful(value)] for value in values)
                    facts.extend(["definition", _meaningful(value)] for value in item.get("tools", [])
                                 if name != "describe_tools")
                else:
                    failures.append((_key([action, "empty"]), {
                        "code": "EMPTY_DISCOVERY", "message": "重复检索没有新结果"}))
            elif item.get("status") in {"prepared", "preview_generated"}:
                # Preview is a proposal, never a new observed vehicle state.
                pass
            elif "data" in item and item["data"] is not None:
                facts.append([item.get("tool_id", name), _meaningful(item["data"])])

        diagnostic = None
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,revision FROM tasks WHERE id=?", (tid,)).fetchone()
            if not current or current["status"] != "running" or current["revision"] != revision:
                return None
            new_fact = False
            for fact in facts:
                inserted = db.execute("INSERT OR IGNORE INTO loop_policy_facts VALUES(?,?,?)",
                                      (tid, revision, _key(fact))).rowcount
                new_fact = new_fact or bool(inserted)
            if new_fact:
                db.execute("DELETE FROM loop_policy_attempts WHERE task_id=? AND revision=?", (tid, revision))
            for fingerprint, error in failures:
                db.execute("""INSERT INTO loop_policy_attempts VALUES(?,?,?,1)
                    ON CONFLICT(task_id,revision,fingerprint)
                    DO UPDATE SET attempts=attempts+1""", (tid, revision, fingerprint))
                count = db.execute("""SELECT attempts FROM loop_policy_attempts
                    WHERE task_id=? AND revision=? AND fingerprint=?""", (tid, revision, fingerprint)).fetchone()[0]
                if count >= self.limit and diagnostic is None:
                    category, instruction = _classification(error)
                    diagnostic = {"code": "NO_PROGRESS", "task_id": tid, "task_revision": revision,
                                  "name": name, "attempts": count, "classification": category,
                                  "error": error, "instruction": instruction}
        if diagnostic:
            question = (f"相同操作已重复 {diagnostic['attempts']} 次且没有新进展，已暂停。"
                        f"{diagnostic['instruction']} 请补充信息或确认是否稍后重试。")
            try:
                pause = self.store.ask(tid, question, revision=revision)
            except ValueError:
                # ask rechecks revision/status atomically after our transaction.
                current = self.store.task(tid)
                if current["revision"] != revision or current["status"] != "running":
                    return None
                raise
            diagnostic.update(status=pause["status"], question=pause["question"])
        return diagnostic
