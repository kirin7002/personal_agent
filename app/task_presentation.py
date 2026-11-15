"""User-visible execution summaries. Never reads provider reasoning or hidden model text."""
import json

from .store import TERMINAL
from .task_observation import history_observation

PRELOAD_REASONS = {
    "disabled": "关闭预加载，按需发现工具", "continuation": "继续原任务", "complex_or_query": "尚未预加载该组合或查询所需的工具",
    "low_confidence": "候选不足，继续浏览目录", "query_domain": "通过目录查找查询能力", "ambiguous_candidates": "候选存在歧义，继续查找",
    "too_many_candidates": "候选较多，继续查找", "schema_budget": "按需读取参数定义",
}


def describe_step(name, args, result, snapshot):
    """Explain observable actions/results only; no invented rationale."""
    error = result.get("error")
    status = "error" if error else "success"
    if error:
        return "调用未通过，检查结果", error.get("message", "工具返回错误"), status
    if name == "search_capabilities":
        return "全局检索车辆能力", f"检索“{args.get('query','')}”，返回 {len(result.get('matches',[]))} 个候选", status
    if name == "browse_capabilities":
        path = tuple(args.get("path", []))
        label = snapshot.directories.get(path, {}).get("title", "根目录" if not path else " / ".join(path))
        return "浏览能力目录", f"{label} · 返回 {len(result.get('items', []))} 项", status
    if name == "describe_tools":
        titles = [snapshot.tools.get(tid, {}).get("title", tid) for tid in args.get("tool_ids", [])]
        return "读取工具参数定义", "、".join(titles), status
    if name == "call_tools":
        receipts = result.get("results", [])
        kinds = {r.get("kind") for r in receipts}
        title = "校验并生成控制预览" if kinds == {"control"} else "查询模拟数据" if kinds <= {"manual", "status"} else "调用所选能力"
        details = []
        failed = 0
        for receipt in receipts:
            label = snapshot.tools.get(receipt.get("tool_id"), {}).get("title", receipt.get("tool_id", "工具"))
            if receipt.get("status") == "error" or receipt.get("error"):
                failed += 1
                detail = (receipt.get("error") or {}).get("message", "调用未通过")
            else:
                detail = "参数通过校验" if receipt.get("status") == "prepared" else "已返回模拟数据"
            details.append(f"{label}：{detail}")
        if failed:
            status = "error" if failed == len(receipts) else "partial"
        return title, "；".join(details) or "未取得有效调用结果", status
    if name == "request_user_input":
        return "请求补充信息", "已向你提出需要澄清的问题" if not error else "未能提交追问", status
    if name == 'wait_for_event':
        return '登记一次等待', '到期后继续原任务；等待期间不调用模型', status
    if name == "finish_task":
        labels = {"generated": "已完成模拟控制预览", "completed": "已完成模拟查询", "partial": "部分完成，已列出未完成项", "unsupported": "已根据证据说明无法完成", "failed": "任务未完成"}
        return "确认并提交结论", labels.get(result.get("status"), "现有证据不足，继续处理"), status
    return "处理任务", "", status


def build_process(task, events, snapshot=None, receipts=()):
    started = task["created"]
    terminal = task["status"] in TERMINAL
    end = task["updated"] if terminal else None
    steps = []
    local_ms = wait_ms = human_ms = 0.0

    def add(ident, title, detail, when, duration_ms, duration_kind, status="success", live=False):
        steps.append({"id": str(ident), "title": title, "detail": detail, "started_at": when, "duration_ms": None if live else round(max(0, duration_ms or 0), 3), "duration_kind": duration_kind, "status": status, "live": live, "at_ms": round(max(0, (when-started)*1000), 3)})

    titles = snapshot.tools if snapshot else {}
    by_time = sorted(events, key=lambda e: (e["created"], e["id"]))
    for event in by_time:
        kind, data, at = event["kind"], event["data"], event["created"]
        if kind=='task.expired':
            add(event['id'],'等待补充已过期','五分钟未收到有效回答，本次任务已结束；旧问题和旧结果不再生效',at,0,'instant','notice')
        elif kind in {'task.waiting','task.followup_queued','task.followup_started','tool.reused'}:
            label = {'task.waiting':'登记等待','task.followup_queued':'收件箱：排队追加','task.followup_started':'开始追加请求','tool.reused':'复用有效控制回执'}[kind]
            add(event['id'],label,data.get('message',''),at,0,'instant')
        elif kind == 'task.goal_revised':
            add(event['id'], '接收改口并更新原任务', data.get('goal','')+'；旧参数、旧控制回执和旧通知不再用于完成当前目标', at, 0, 'instant')
        elif kind.startswith('task.steer_'):
            revision_event = next((e for e in by_time if e['kind']=='task.goal_revised' and e['data'].get('task_revision')==data.get('task_revision')), None)
            began = revision_event['created'] if revision_event else at
            add(event['id'], '将新目标送入 Hermes', data.get('message',''), began, (at-began)*1000, 'stage_interval', 'success' if kind in {'task.steer_accepted','task.steer_started'} else 'error')
        elif kind == 'task.stale_output_blocked':
            add(event['id'], '拦截旧目标参数', data.get('message',''), at, 0, 'instant', 'notice')
        elif kind == "catalog.preloaded":
            ms = float(data.get("elapsed_ms", 0))
            labels = [titles.get(tid, {}).get("title", tid) for tid in data.get("tool_ids", [])]
            add(event["id"], "预加载候选工具", "已提供候选工具的完整参数定义："+"、".join(labels)+"。是否使用仍由 Agent 根据目标判断。", at-ms/1000, ms, "measured_local")
            related = [titles.get(tid, {}).get("title", tid) for tid in data.get("related_tool_ids", [])]
            steps[-1]["observation"] = {"title":"预加载候选工具", "action":"明确目标，仅预加载控制定义" if data.get("preload_policy") == "primary_only" else "本地召回候选控制与相关查询，提前读取参数规则", "input":task.get("goal", ""),
                "output":"控制候选："+("、".join(labels) or "无")+"；查询候选："+("、".join(related) or "无")+"。这些是工具说明，不是已查询的车辆数据。", "data":json.dumps({"control_candidates":data.get("tool_ids",[]),"query_candidates":data.get("related_tool_ids",[]),"directory_hints":data.get("directory_hints",[]),"payload_chars":data.get("payload_chars")},ensure_ascii=False,indent=2)}
            local_ms += ms
        elif kind == "catalog.preload_skipped":
            add(event["id"], "选择处理路径", PRELOAD_REASONS.get(data.get("reason"), "继续完整目录流程"), at, data.get("elapsed_ms", 0), "measured_local")
            local_ms += float(data.get("elapsed_ms", 0))
        elif kind == "task.outcome_resolved":
            label = {"prepared":"纯车控", "answered":"纯查询", "mixed":"查询＋车控"}.get(data.get("resolved"), "成功结果")
            add(event["id"], "程序判定结论类型", f"{data.get('control_results',0)} 条控制回执、{data.get('query_results',0)} 条查询回执 → {label}；无需模型修正类型", at, 0, "instant")
        elif kind == "task.fast_completed":
            add(event["id"], "校验通过，直接通知前台", "本地完成判定 · 省去确认结果的模型请求", at, 0, "instant")
        elif kind == "catalog.fallback":
            add(event["id"], "补充检索工具" if data.get("reason") == "additional_search" else "转入完整目录查证", {"tool_error":"已有调用返回错误，退出直接完成路径", "additional_search":"Agent 请求全局搜索，补充所需工具定义", "additional_discovery":"Agent 请求浏览目录，继续寻找所需能力", "additional_schema":"Agent 请求读取更多工具的参数定义"}.get(data.get("reason"),"继续由 Agent 处理"), at, 0, "instant", "notice")

    instrumented = [e for e in by_time if e["kind"] == "agent.step"]
    dispatches = [e for e in by_time if e["kind"] == "task.dispatched"]
    cursor = dispatches[0]["created"] if dispatches else started
    if instrumented:
        previous_revision = None
        for event in sorted(instrumented, key=lambda e: (e["data"]["started_at"], e["id"])):
            d = event["data"]
            revision = d.get("task_revision", 0)
            if previous_revision != revision:
                dispatch = next((e for e in dispatches if e["data"].get("task_revision", 0) == revision), None)
                if dispatch:
                    cursor = dispatch["created"]
                previous_revision = revision
            observed = d.get("observation") or (history_observation(event, by_time, snapshot, receipts) if snapshot else None)
            title = observed.get("title") if observed else d["title"]
            at = d["started_at"]
            gap = max(0, (at-cursor)*1000)
            if gap >= 5:
                add(f"wait-{event['id']}", "模型与服务等待", f"本轮返回的操作：{title}", cursor, gap, "boundary_wait")
                wait_ms += gap
            ms = d["duration_ms"]
            add(event["id"], title, d["detail"], at, ms, "measured_tool", d.get("status", "success"))
            if observed:
                steps[-1]["observation"] = observed
            local_ms += ms
            cursor = max(cursor, at+ms/1000)
    else:
        # Historical records have completion timestamps, not local execution timers.
        # Their intervals include model/network time and are explicitly labeled as such.
        paused = False
        for event in by_time:
            kind, d, at = event["kind"], event["data"], event["created"]
            if kind in {"task.dispatched", "task.resumed"}:
                cursor = at
                paused = False
                continue
            label = detail = None
            if kind == "catalog.browsed":
                path = tuple(d.get("path", []))
                label = "决策并浏览目录"
                detail = snapshot.directories.get(path, {}).get("title", "根目录" if not path else " / ".join(path)) if snapshot else " / ".join(path)
            elif kind == "catalog.described":
                label = "选择工具并读取参数定义"
                detail = "、".join(titles.get(tid, {}).get("title", tid) for tid in d.get("tool_ids", []))
            elif kind in {"tool.preview", "tool.mock", "tool.invalid"}:
                label = {"tool.preview": "生成参数并完成校验", "tool.mock": "取得模拟查询结果", "tool.invalid": "调用未通过，继续查证"}[kind]
                tid = d.get("tool_id") or d.get("call", {}).get("tool", "")
                detail = titles.get(tid, {}).get("title", tid)
            elif kind == "task.question":
                label, detail = "提出澄清问题", "等待你补充必要信息"
            elif kind in {"task.generated", "task.completed", "task.partial", "task.unsupported", "task.failed", "task.cancelled", "task.interrupted"}:
                label, detail = "确认任务结果", "依据实际调用记录提交结论" if kind in {"task.generated", "task.completed"} else "任务已停止或未能全部完成"
            if label:
                if paused and kind in {"task.cancelled", "task.failed", "task.interrupted"}:
                    add(event["id"], label, detail, at, 0, "instant")
                    continue
                ms = max(0, (at-cursor)*1000)
                add(event["id"], label, detail, cursor, ms, "stage_interval", "error" if kind in {"tool.invalid", "task.failed"} else "success")
                wait_ms += ms
                cursor = at
                paused = kind == "task.question"

    # A resumed run can be awaiting its first tool event. Do not count the
    # intervening user-answer pause as model/service time.
    resumes = [e for e in by_time if e["kind"] == "task.resumed"]
    if resumes and resumes[-1]["created"] > cursor:
        cursor = resumes[-1]["created"]
    if dispatches and dispatches[-1]["created"] > cursor:
        cursor = dispatches[-1]["created"]
    questions = [e for e in by_time if e["kind"] == "task.question"]
    ended_while_waiting = False
    for question in questions:
        resumed = next((e for e in resumes if e["created"] > question["created"]), None)
        if resumed or terminal:
            wait_end = resumed["created"] if resumed else end
            ms = max(0, wait_end-question["created"])*1000
            add(f"human-{question['id']}", "等待你的补充", "收到回答后继续原任务" if resumed else "等待期间任务结束", question["created"], ms, "human_wait")
            human_ms += ms
            if not resumed:
                ended_while_waiting = True

    if terminal and instrumented and not ended_while_waiting and end and end > cursor+.005:
        gap = (end-cursor)*1000
        add("terminal-wait", "等待任务结束", "包含模型、网络及服务调度", cursor, gap, "boundary_wait")
        wait_ms += gap
    elif task['status'] == 'waiting':
        waiting = [e for e in by_time if e['kind']=='task.waiting']
        at = waiting[-1]['created'] if waiting else task['updated']
        label = '等待时间到达' if waiting and waiting[-1]['data'].get('kind')=='timer' else '等待外部结果'
        add('live-wait',label,'等待期间不调用模型；条件满足后继续原任务',at,None,'event_wait','waiting',True)
    elif task["status"] == "awaiting_input":
        at = questions[-1]["created"] if questions else task["updated"]
        add("live-human", "等待你的补充", "回答后将继续当前任务", at, None, "human_wait", "waiting", True)
    elif task["status"] == "running":
        add("live-model", "等待模型选择下一步" if dispatches else "正在连接任务服务", "收到真实工具事件后更新", cursor, None, "boundary_wait", "running", True)
    steps.sort(key=lambda s: (s["started_at"], s["id"]))
    return {"kind": "execution_summary", "steps": steps, "timing": {"total_ms": round((end-started)*1000, 3) if end else None, "local_measured_ms": round(local_ms, 3), "model_service_interval_ms": round(wait_ms, 3), "human_wait_ms": round(human_ms, 3), "precise_tool_timers": bool(instrumented), "direct_completion": any(e["kind"] == "task.fast_completed" for e in by_time), "provider_requests": sum(e["kind"] == "agent.model_request" for e in by_time) if any(e["kind"] == "agent.model_request" for e in by_time) else None}, "notice": "基于实际调用事件的执行摘要；阶段区间含模型、网络与调度，不是模型内部推理。"}
