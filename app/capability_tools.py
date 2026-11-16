"""Stable Agent-facing tools over a versioned, configuration-only capability tree."""
from copy import deepcopy
import json
import time
import uuid
import sqlite3
from contextlib import suppress

from jsonschema import Draft202012Validator

from .capability_catalog import CapabilityCatalog, CatalogError, json_read, mock_data, pointer
from .store import Store, TERMINAL


def entrypoint_schemas():
    identity = {"task_id": {"type": "string"}, "task_revision": {"type": "integer", "minimum": 0}}
    def spec(name, description, properties, required=()):
        return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": {**identity, **properties}, "required": ["task_id", "task_revision", *required], "additionalProperties": False}}
    text = {"type": "string", "minLength": 1}
    return [
        spec("search_capabilities", "跨全部车辆目录直接检索所需能力，支持中文或英文关键词。返回少量匹配工具、真实路径及预算内的完整 Schema；有完整定义可直接 call_tools，无需再逐层浏览或 describe。只查工具说明，不读取车况或执行车控。零结果不代表车辆不支持，可换词或浏览目录。", {"query": {"type":"string","minLength":1,"maxLength":300}, "kind":{"type":"string","enum":["control","manual","status"]}, "limit":{"type":"integer","minimum":1,"maximum":6}}, ["query"]),
        spec("browse_capabilities", "浏览车辆能力目录。path=[] 查看顶层；返回本层类别或工具摘要，不返回完整参数。可以跨域或同时浏览多个类别。query 是中英文名称/别名子串过滤，非语义搜索；找不到时去掉 query 浏览。", {"path": {"type": "array", "items": {"type": "string"}, "maxItems": 8}, "query": {"type": "string", "maxLength": 100}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        spec("describe_tools", "获取选中业务工具的完整说明、版本、输入输出 Schema、示例与模拟前置条件。参数定义以此结果为准。", {"tool_ids": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": text}}, ["tool_ids"]),
        spec("call_tools", "按工具的具体 Schema 提交调用。车控只生成结构；车书/车况返回带来源的模拟数据。每项返回 result_id，完成时引用它。每轮 call_id 唯一；相同请求重试返回原结果，修正参数须使用新 call_id。", {
            "catalog_revision": text,
            "complete_if_success": {"type": "boolean", "description": "仅在任务允许快速完成且本次单项控制完整覆盖用户目标、没有其他待办时设为 true；失败时仍继续查证，默认 false。"},
            "calls": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {"call_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"}, "tool_id": text, "tool_version": text, "arguments": {"type": "object"}}, "required": ["call_id", "tool_id", "tool_version", "arguments"], "additionalProperties": False}},
        }, ["catalog_revision", "calls"]),
        spec("request_user_input", "必要参数缺失或用户目标有歧义时，向用户提出一个明确问题。调用后结束本轮等待补充，不自行猜测关键参数。", {"question": {"type": "string", "minLength": 1, "maxLength": 500}}, ["question"]),
        spec("wait_for_event", "登记一次定时等待，seconds 从任务接收时计时。返回 waiting 后结束当前轮，不轮询也不继续控制；时间到后系统继续同一任务。不要重复登记已到期的等待。", {
            "wait_id":{"type":"string","minLength":1,"maxLength":80},
            "seconds":{"type":"number","minimum":0,"maximum":60},
            "continuation":{"type":"string","minLength":1,"maxLength":500}
        }, ["wait_id","seconds","continuation"]),
        spec("finish_task", "目标已完成时，引用实际成功 result_id 并提供摘要即可；省略 outcome 或用 auto，程序根据回执判定纯车控、纯查询或混合结果。未完成时仍显式使用 unsupported/partial/failed；partial 必须列出 unresolved。程序不会将错误或未完成项自动变成成功，不得把模拟结果说成真实执行。", {
            "outcome": {"type": "string", "enum": ["auto", "prepared", "answered", "mixed", "unsupported", "partial", "failed"], "default": "auto"},
            "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
            "result_ids": {"type": "array", "maxItems": 64, "uniqueItems": True, "items": text},
            "unresolved": {"type": "array", "maxItems": 16, "items": text},
        }, ["summary", "result_ids"]),
    ]


class CapabilityTools:
    def __init__(self, store: Store, catalog: CapabilityCatalog):
        self.store, self.catalog = store, catalog
        from .capability_search import CapabilitySearch
        self.searcher = CapabilitySearch()
        from .loop_policy import LoopPolicy
        self.loop_policy = LoopPolicy(store)
        self.validators = {s["name"]: Draft202012Validator(s["inputSchema"]) for s in entrypoint_schemas()}
        self.result_validator = Draft202012Validator(json_read(catalog.root / "schemas/result.schema.json"))

    @staticmethod
    def error(code, message, details=None):
        return {"error": {"code": code, "message": message, "details": details or []}, "simulated": True, "executed": False}

    def context(self, tid, revision):
        task = self.store.task(tid)
        if task["status"] != "running" or task["revision"] != revision:
            raise CatalogError("TASK_INACTIVE：任务已暂停、取消或轮次过期")
        if not task.get("catalog_revision"):
            raise CatalogError("旧版任务不能使用新目录协议，请创建新任务")
        return task, self.catalog.snapshot(task["catalog_revision"])

    def call(self, name, arguments):
        started_at, started_clock = time.time(), time.perf_counter()
        context = None
        validator = self.validators.get(name)
        if validator and validator.is_valid(arguments):
            with suppress(KeyError, ValueError):
                context = self.context(arguments["task_id"], arguments["task_revision"])
        result = self._call(name, arguments)
        if context:
            policy = self.loop_policy.observe(context[0], name, arguments, result)
            if policy:
                result = {**result, 'loop_status':policy}
        duration_ms = (time.perf_counter()-started_clock)*1000
        if context:
            from .task_presentation import describe_step
            task, snapshot = context
            from .task_observation import observation
            observed = None
            with suppress(ValueError, TypeError, AttributeError, KeyError):
                observed = observation(name, arguments, result, snapshot)
            title, detail, status = describe_step(name, arguments, result, snapshot)
            # Observability must never convert a committed tool result into a retry.
            with suppress(sqlite3.Error, KeyError):
                self.store.event(task["id"], "agent.step", {"task_revision": task["revision"], "name": name, "title": title, "detail": detail, "status": status, "started_at": started_at, "duration_ms": round(duration_ms, 3), "observation": observed})
        return result

    def _call(self, name, arguments):
        validator = self.validators.get(name)
        if validator is None:
            return self.error("UNKNOWN_ENTRYPOINT", "未知协议入口")
        errors = list(validator.iter_errors(arguments))
        if errors:
            return self.error("INVALID_REQUEST", "通用协议参数不合法", [{"path": "/"+"/".join(map(str,e.path)), "message": e.message} for e in errors[:8]])
        args = deepcopy(arguments)
        tid, revision = args.pop("task_id"), args.pop("task_revision")
        try:
            task, snap = self.context(tid, revision)
            if name == "search_capabilities":
                result = self.searcher.search(snap, **args)
                self.store.preload_fallback(tid, "additional_search")
                self.store.event(tid, "catalog.searched", {"query":result["query"], "domain":args.get("kind"), "tool_ids":[m["id"] for m in result["matches"]], "schema_ids":[t["id"] for t in result["tools"]], "elapsed_ms":result["elapsed_ms"], "method":result["method"]})
                return result
            if name == "browse_capabilities":
                self.store.preload_fallback(tid, "additional_discovery")
                result = snap.browse(**args)
                self.store.event(tid, "catalog.browsed", {"path": result["path"], "count": len(result["items"])})
                return result
            if name == "describe_tools":
                self.store.preload_fallback(tid, "additional_schema")
                self.store.event(tid, "catalog.described", {"tool_ids": args["tool_ids"]})
                return snap.describe(args["tool_ids"])
            if name == "request_user_input":
                if not args["question"].strip():
                    return self.error("INVALID_REQUEST", "追问不能为空白")
                return self.store.ask(tid, args["question"].strip(), revision)
            if name == 'wait_for_event':
                from .loop_events import LoopEvents
                wait = LoopEvents(self.store).register_wait(task,'timer',args['wait_id'],delay_seconds=args['seconds'],continuation=args['continuation'])
                return {'status':'waiting','wait_id':wait['wait_id'],'deadline':wait['deadline'],'instruction':'结束当前轮，等待应用到期继续；不表示任务完成'}
            if name == "finish_task":
                args.setdefault("outcome", "auto")
                return self.finish(task, snap, **args)
            if args["catalog_revision"] != snap.revision:
                return self.error("CATALOG_VERSION_MISMATCH", "请使用当前任务固定的目录版本")
            results = [self.invoke(tid, revision, snap, call) for call in args["calls"]]
            response = {"catalog_revision": snap.revision, "results": results}
            if args.get("complete_if_success") is True and len(results) == 1 and results[0].get("status") == "prepared":
                completion = self.finish(task, snap, "prepared", "", [results[0]["result_id"]], fast_arguments=arguments)
                if completion.get("status") == "generated":
                    response["completion"] = completion
                else:
                    response["completion"] = {"status": "continue", "instruction": "未满足快速完成条件，请继续原流程并以 finish_task 提交结论。"}
            if any(r.get("status") == "error" and r.get("kind") == "control" for r in results):
                response["recovery"] = {"action": "continue_catalog_or_clarify", "message": "需要查证失败原因时，通过目录取得相关查询证据；参数不明确或无效时可向用户追问。", "query_roots": [{"path": d["path"], "title": d["title"]} for d in snap.directories.values() if len(d["path"]) == 1 and d["path"][0] != "control"]}
            return response
        except (KeyError, CatalogError, ValueError) as exc:
            return self.error("INVALID_CONTEXT", str(exc))

    def invoke(self, tid, revision, snap, request):
        encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            if task["status"] != "running" or task["revision"] != revision:
                return self.error("TASK_INACTIVE", "任务已暂停、取消或轮次过期")
            from .goal_revision import TOOL as TEMPERATURE_TOOL, TEMP, zone, number
            if (task['source']=='hermes' and request['tool_id']==TEMPERATURE_TOOL and '空调' in task['goal']
                    and Draft202012Validator(snap.tools.get(TEMPERATURE_TOOL,{}).get('input_schema',False)).is_valid(request['arguments'])
                    and len(TEMP.findall(task['goal']))==1 and not any(word in task['goal'] for word in ('增加','升高','降低','减少'))):
                if request['arguments'].get('temperature_c') != number(TEMP.findall(task['goal'])[0]):
                    return self.error('OUTDATED_TARGET','生成温度与用户明确目标不一致；不能擅自改写目标值。')
                try:
                    confirmed_zone = zone(task['goal'])
                    if not confirmed_zone:
                        for entry in reversed(task['history']):
                            confirmed_zone = zone(entry.get('answer',''))
                            if confirmed_zone:
                                break
                except ValueError:
                    confirmed_zone = None
                if not confirmed_zone:
                    question = '要设置哪个分区：主驾、副驾、后排还是全部？'
                    db.execute("UPDATE tasks SET status='awaiting_input',question=?,updated=? WHERE id=?",(question,time.time(),tid))
                    self.store._event(db,task,'task.question',{'message':question},True)
                    return self.error('NEEDS_USER_INPUT','分区尚未由用户确认，已暂停并追问；不能猜测为全部。')
                if request['arguments'].get('zone') != confirmed_zone:
                    return self.error('OUTDATED_TARGET','分区与用户已确认的范围不一致，请按原目标生成。')
            change = self.store.latest_change(tid, db)
            tool_definition = snap.tools.get(request['tool_id'], {})
            if (tool_definition.get('kind') == 'control' and tool_definition.get('enabled')
                    and request['tool_version'] == tool_definition['version']
                    and Draft202012Validator(tool_definition['input_schema']).is_valid(request['arguments'])):
                guarded = self._guard_simple_control(db, task, snap, tool_definition, request['arguments'])
                if guarded:
                    return guarded
            if change and tool_definition.get('kind') == 'control':
                expected = change['constraints']
                if not expected or request['tool_id'] != expected.get('tool_id') or request['arguments'] != expected.get('arguments'):
                    self.store._event(db, task, 'task.stale_output_blocked', {'message':'该控制不符合已确认的新目标，已拦截', 'tool_id':request['tool_id']})
                    return self.error('OUTDATED_TARGET', '控制参数不符合最新确认目标；请按最新约束重新生成，不能复用旧参数。')
            alias = db.execute('SELECT a.request,c.response FROM capability_aliases a JOIN capability_calls c ON c.id=a.result_id WHERE a.task_id=? AND a.task_revision=? AND a.call_id=?', (tid,revision,request['call_id'])).fetchone()
            if alias:
                return json.loads(alias['response']) if alias['request'] == encoded else self.error('CALL_ID_CONFLICT','同一 call_id 不能使用不同参数')
            old = db.execute("SELECT request,response FROM capability_calls WHERE task_id=? AND task_revision=? AND call_id=?", (tid, revision, request["call_id"])).fetchone()
            if old:
                if old[0] == encoded:
                    return json.loads(old[1])
                return self.error("CALL_ID_CONFLICT", "同一 call_id 不能使用不同参数，请使用新 call_id")
            if tool_definition.get('kind') == 'control':
                for row in db.execute('SELECT response FROM capability_calls WHERE task_id=? AND task_revision=? AND tool_id=?', (tid,revision,request['tool_id'])):
                    receipt = json.loads(row['response'])
                    if receipt.get('status') == 'prepared' and receipt.get('tool_version') == request['tool_version'] and receipt.get('arguments') == request['arguments']:
                        db.execute('INSERT INTO capability_aliases VALUES(?,?,?,?,?)',(tid,revision,request['call_id'],encoded,receipt['result_id']))
                        self.store._event(db,task,'tool.reused',{'message':'相同目标与参数已生成，复用原控制回执','result_id':receipt['result_id']})
                        return receipt
            if db.execute("SELECT count(*) FROM capability_calls WHERE task_id=?", (tid,)).fetchone()[0] >= 64:
                return self.error("CALL_LIMIT", "当前任务已达到 64 次业务调用上限")
            profile = snap.profiles[task["profile_id"]]
            tool = snap.tools.get(request["tool_id"])
            result = {
                "result_id": "res_"+uuid.uuid4().hex, "call_id": request["call_id"],
                "tool_id": request["tool_id"], "tool_version": request["tool_version"],
                "catalog_revision": snap.revision, "kind": tool["kind"] if tool else None,
                "status": "error", "simulated": True, "executed": False, "arguments": request["arguments"],
                "data": None, "error": None, "provenance": {"profile_id": profile["id"], "source": "mock_configuration", "sample_observed_at": profile["data"].get("sample_observed_at")},
            }
            def reject(code, message, details=None):
                result["error"] = {"code": code, "message": message, "details": details or []}
            if tool is None:
                reject("TOOL_NOT_FOUND", "目录中不存在该工具；这不等于车辆未配备")
            elif not tool["enabled"]:
                reject("TOOL_DISABLED", "该工具在配置中已停用")
            elif request["tool_version"] != tool["version"]:
                reject("TOOL_VERSION_MISMATCH", "工具版本与当前任务快照不一致")
            else:
                errors = list(Draft202012Validator(tool["input_schema"]).iter_errors(request["arguments"]))
                if errors:
                    reject("INVALID_ARGUMENTS", "参数未通过业务 Schema 校验", [{"path": "/"+"/".join(map(str,e.path)), "message": e.message} for e in errors[:8]])
                else:
                    for condition in tool.get("preconditions", []):
                        actual = pointer(profile["data"], condition["pointer"])
                        if actual != condition["equals"] or type(actual) is not type(condition["equals"]):
                            reject(condition["code"], condition["message"], [{"pointer": condition["pointer"], "actual": actual, "expected": condition["equals"]}])
                            break
                    if not result["error"]:
                        try:
                            data = mock_data(tool, request["arguments"], profile)
                            if not Draft202012Validator(tool["output_schema"]).is_valid(data):
                                reject("INVALID_MOCK_OUTPUT", "模拟数据未通过结果 Schema 校验")
                            else:
                                result["status"] = "prepared" if tool["kind"] == "control" else "returned_mock"
                                result["data"] = data
                        except CatalogError as exc:
                            reject("NO_MOCK_MATCH", str(exc))
            db.execute("INSERT INTO capability_calls VALUES(?,?,?,?,?,?,?,?)", (result["result_id"], tid, revision, request["call_id"], request["tool_id"], encoded, json.dumps(result, ensure_ascii=False), time.time()))
            if result["status"] == "error":
                self.store._preload_fallback(db, task, "tool_error")
            if result["status"] == "prepared":
                calls = task["calls"]
                proposal = {"tool": tool["id"], "version": tool["version"], "arguments": request["arguments"], "result_id": result["result_id"]}
                if not any(c["tool"] == proposal["tool"] and c["arguments"] == proposal["arguments"] for c in calls):
                    calls.append(proposal)
                    db.execute("UPDATE tasks SET calls=?,updated=? WHERE id=?", (json.dumps(calls, ensure_ascii=False), time.time(), tid))
            self.store._event(db, task, "tool.preview" if result["status"] == "prepared" else "tool.mock" if result["status"] == "returned_mock" else "tool.invalid", {"tool_id": request["tool_id"], "status": result["status"], "result_id": result["result_id"], "error": result["error"]})
            return result

    @staticmethod
    def _simple_context(task, snap):
        from .control_hints import object_matches
        from .tool_preload import complex_request
        if task['source'] != 'hermes' or complex_request(task['goal']):
            return '', {}
        # User answers are evidence, never generated assistant questions/text.
        text = '\n'.join([task['goal'], *[h['answer'] for h in task['history'] if isinstance(h.get('answer'), str)]])
        relevant = {tid: t for tid, t in snap.tools.items() if t['enabled'] and t['kind'] == 'control'
                    and object_matches(t, task['goal'])}
        return text, relevant

    def _ask_control(self, db, task, question):
        # Same transition as Store.ask, within the already locked invocation.
        db.execute("UPDATE tasks SET status='awaiting_input',question=?,updated=? WHERE id=?",
                   (question, time.time(), task['id']))
        self.store._event(db, task, 'task.question', {'message': question}, True)
        return self.error('NEEDS_USER_INPUT', '目标信息未明确，已暂停并追问；不能猜测参数。')

    def _guard_simple_control(self, db, task, snap, tool, arguments):
        from .control_hints import assess, recall_matches
        text, relevant = self._simple_context(task, snap)
        if not relevant:
            return None
        expected = [tid for tid, t in relevant.items() if recall_matches(t, text) is True]
        if len(expected) == 1 and tool['id'] != expected[0]:
            return self.error('CONTROL_TARGET_MISMATCH', '该工具与明确的单项控制目标不符，请使用匹配的控制工具。',
                              [{'expected_tool_id': expected[0]}])
        if tool['id'] not in relevant:
            return None
        assessment = assess(tool, text)
        if assessment['excluded']:
            return self.error('CONTROL_TARGET_MISMATCH', '目标包含该工具的排除条件，不能用全开或全关替代部分开度等其他操作。')
        for field, target in assessment['constraints'].items():
            allowed = target if isinstance(target, list) else [target]
            if arguments.get(field) not in allowed:
                return self.error('OUTDATED_TARGET', '生成参数与用户明确目标不一致；不能改写、限幅或猜测目标。', [{'field': field}])
        if assessment['missing']:
            return self._ask_control(db, task, assessment['missing'][0]['question'])
        if assessment['relative']:
            query_id = tool['input_schema']['x-agent'].get('relative_tool')
            if not snap.tools.get(query_id, {}).get('enabled'):
                return self._ask_control(db, task, '当前没有可用的状态查询，请明确希望设置的绝对目标。')
            receipts = (json.loads(row[0]) for row in db.execute(
                'SELECT response FROM capability_calls WHERE task_id=? AND task_revision=? AND tool_id=?',
                (task['id'], task['revision'], query_id)))
            if not any(r['status'] == 'returned_mock' and r['kind'] == 'status' for r in receipts):
                return self.error('NEEDS_RELATIVE_QUERY', '相对调整必须先读取本任务当前轮次的相关状态，再决定绝对目标。',
                                  [{'relative_tool_id': query_id}])
        return None

    def _guard_simple_finish(self, db, task, snap, controls):
        from .control_hints import assess, recall_matches
        text, relevant = self._simple_context(task, snap)
        expected = [t for t in relevant.values() if recall_matches(t, text) is True]
        if len(expected) == 1 and not any(r['tool_id'] == expected[0]['id'] for r in controls):
            return self.error('CONTROL_TARGET_INCOMPLETE', '缺少当前单项控制目标的成功回执，不能报告完成。')
        for receipt in controls:
            tool = snap.tools[receipt['tool_id']]
            guarded = self._guard_simple_control(db, task, snap, tool, receipt['arguments'])
            if guarded:
                return guarded
        for tid in {r['tool_id'] for r in controls} & relevant.keys():
            assessment = assess(relevant[tid], text)
            scopes = {field: target for field, target in assessment['constraints'].items() if isinstance(target, list)}
            if not scopes:
                continue
            # Count actual combinations rather than expanding unbounded products.
            required = 1
            for values in scopes.values():
                required *= len(values)
            covered = {tuple(r['arguments'].get(field) for field in scopes)
                       for r in controls if r['tool_id'] == tid}
            if len(covered) != required:
                return self.error('CONTROL_TARGET_INCOMPLETE', '必须覆盖用户明确要求的全部位置，不能把一个位置的成功当作全部完成。')
        return None

    def finish(self, initial, snap, outcome, summary, result_ids, unresolved=None, fast_arguments=None):
        unresolved = unresolved or []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.store.decode(db.execute("SELECT * FROM tasks WHERE id=?", (initial["id"],)).fetchone())
            if task["status"] != "running" or task["revision"] != initial["revision"]:
                return self.error("TASK_INACTIVE", "任务已改变，不能提交旧结论")
            rows = db.execute("SELECT task_revision,response FROM capability_calls WHERE task_id=? ORDER BY created,rowid", (task["id"],)).fetchall()
            change = self.store.latest_change(task['id'], db)
            obsolete = {json.loads(r['response'])['result_id'] for r in rows
                        if change and r['task_revision'] < change['revision'] and json.loads(r['response']).get('kind') == 'control'}
            if obsolete.intersection(result_ids):
                return self.error('SUPERSEDED_EVIDENCE', '旧目标的控制回执已失效，不能完成当前目标。')
            records = {r['result_id']:r for r in (json.loads(row['response']) for row in rows) if r['result_id'] not in obsolete}
            if fast_arguments is not None:
                from .direct_completion import can_complete
                events = [{"kind": r[0], "data": json.loads(r[1])} for r in db.execute("SELECT kind,data FROM events WHERE task_id=? ORDER BY id", (task["id"],))]
                if not can_complete(task, events, records, fast_arguments):
                    return self.error("CONTINUE_AGENT", "当前任务需由 Agent 继续处理")
            if any(rid not in records for rid in result_ids):
                return self.error("INVALID_EVIDENCE", "result_ids 必须来自本任务实际获得的结果")
            selected = [records[rid] for rid in result_ids]
            controls = [r for r in selected if r["status"] == "prepared"]
            queries = [r for r in selected if r["status"] == "returned_mock"]
            errors = [r for r in selected if r["status"] == "error"]
            if outcome in {'auto', 'prepared', 'mixed', 'answered'}:
                guarded = self._guard_simple_finish(db, task, snap, controls)
                if guarded:
                    return guarded
            if change and outcome in {'auto','prepared','mixed','answered'}:
                expected = change['constraints']
                if not controls or any(r['tool_id'] != expected.get('tool_id') or r['arguments'] != expected.get('arguments') for r in controls):
                    return self.error('NEW_TARGET_NOT_SATISFIED', '没有满足最新确认目标的控制回执，不能报告完成。')
            operational_failures = [r for r in records.values() if r["kind"] == "control" and r["status"] == "error" and r["error"]["code"] not in {"INVALID_ARGUMENTS", "TOOL_VERSION_MISMATCH", "INVALID_MOCK_OUTPUT", "NO_MOCK_MATCH"}]
            has_query_capabilities = any(t["enabled"] and t["kind"] != "control" for t in snap.tools.values())
            if task["source"] == "hermes" and outcome in {"unsupported", "partial", "failed"} and operational_failures and has_query_capabilities and not queries:
                return self.error("NEEDS_DIAGNOSTIC_EVIDENCE", "车控失败后请先通过目录补充相关查询证据，或向用户追问，再提交结论。")
            if outcome == "auto":
                ordered = list(records.values())
                positions = {r["result_id"]: index for index, r in enumerate(ordered)}
                for failed in ordered:
                    if failed["status"] != "error":
                        continue
                    recovered = any(r["tool_id"] == failed["tool_id"] and r["status"] in {"prepared", "returned_mock"}
                                    and positions[r["result_id"]] > positions[failed["result_id"]] for r in selected)
                    if not recovered:
                        return self.error("EXPLICIT_OUTCOME_REQUIRED", "存在尚未由后续成功回执解决的调用失败，请明确提交失败、不支持或部分完成；不能自动报告全部成功。")
            from .result_classification import resolve_success_outcome
            try:
                resolution = resolve_success_outcome(outcome, controls, queries, errors, unresolved)
            except ValueError as exc:
                return self.error("INVALID_OUTCOME", str(exc))
            if resolution:
                outcome = resolution["resolved"]
            valid = {
                "prepared": bool(controls) and not queries,
                "answered": bool(queries) and not controls,
                "mixed": bool(controls) and bool(queries),
                "unsupported": bool(selected) and not controls,
                "partial": bool(controls or queries) and bool(unresolved),
                "failed": True,
            }[outcome]
            if not valid:
                return self.error("INVALID_OUTCOME", "结论类型与引用证据不一致")
            calls = []
            for r in controls:
                proposal = {"tool": r["tool_id"], "version": r["tool_version"], "arguments": r["arguments"], "result_id": r["result_id"]}
                if not any(c["tool"] == proposal["tool"] and c["arguments"] == proposal["arguments"] for c in calls):
                    calls.append(proposal)
            if len(calls) > 8:
                return self.error("CALL_LIMIT", "一次任务最终最多保留 8 个不同车控预览")
            status = {"prepared": "generated", "answered": "completed", "mixed": "generated", "unsupported": "unsupported", "partial": "partial", "failed": "failed"}[outcome]
            titles = "、".join(dict.fromkeys(snap.tools[c["tool"]]["title"] for c in calls))
            if outcome == "prepared":
                message = f"{titles}：调用结构已通过校验，已模拟下发成功。"
            elif outcome == "mixed":
                message = f"{titles}：已模拟下发成功。模拟查询结论：{summary}"
            elif outcome == "answered":
                message = f"根据模拟查询数据：{summary}"
            elif outcome == "unsupported":
                message = f"根据模拟数据，无法完成请求：{summary}"
            elif outcome == "partial":
                message = f"仅完成部分请求：{summary}。未完成：{'；'.join(unresolved)}"
            else:
                message = f"任务未完成：{summary}"
            final = {"schema_version": "capability-result/v1", "catalog_revision": snap.revision, "profile_id": task["profile_id"], "mode": "mock", "executed": False, "outcome": outcome, "calls": calls, "results": selected, "summary": message, "unresolved": unresolved}
            self.result_validator.validate(final)
            if change and outcome in {'prepared','mixed'}:
                db.execute("UPDATE task_changes SET status='applied',updated=? WHERE task_id=? AND revision=?", (time.time(),task['id'],change['revision']))
            if resolution:
                self.store._event(db, task, "task.outcome_resolved", resolution)
            db.execute("UPDATE tasks SET status=?,outcome=?,calls=?,summary=?,final_result=?,updated=? WHERE id=?", (status, outcome, json.dumps(calls, ensure_ascii=False), message, json.dumps(final, ensure_ascii=False), time.time(), task["id"]))
            if fast_arguments is not None:
                self.store._event(db, task, "task.fast_completed", {"message": "单项控制校验通过，直接通知前台", "task_revision": task["revision"], "completion_source": "local_validator"})
            self.store._event(db, task, "task."+status, {"message": message, "status": status, "outcome": outcome, "simulated": True, "executed": False, "calls": calls}, True)
            return {"status": status, "outcome": outcome, "message": message, "executed": False, **({"type_resolution": resolution} if resolution else {})}
