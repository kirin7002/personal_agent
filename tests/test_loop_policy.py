from concurrent.futures import ThreadPoolExecutor

import pytest

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.loop_policy import LoopPolicy
from app.store import Store


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "loop.db")
    task, _ = store.create_task(store.create_session(), "设置温度为23度", "one")
    return store, task, LoopPolicy(store)


def failure(task, index=0, code="INVALID_ARGUMENTS", temperature=45):
    request = {"call_id": f"call_{index}", "tool_id": "control.climate.set_temperature",
               "tool_version": "1", "arguments": {"zone": "all", "temperature_c": temperature}}
    args = {"task_id": task["id"], "task_revision": task["revision"],
            "catalog_revision": "cat_1", "calls": [request]}
    receipt = {**request, "result_id": f"res_{index}", "status": "error", "kind": "control",
               "error": {"code": code, "message": "无法生成预览", "details": []},
               "data": None, "provenance": {"sample_observed_at": index}, "timestamp": index}
    return args, {"results": [receipt]}


def fail(policy, task, index=0, **kwargs):
    args, result = failure(task, index, **kwargs)
    return policy.observe(task, "call_tools", args, result)


def query(policy, task, temperature, index=0):
    args = {"calls": [{"call_id": f"q_{index}", "tool_id": "status.climate.get_state",
                       "tool_version": "1", "arguments": {}}]}
    result = {"results": [{"tool_id": "status.climate.get_state", "call_id": f"q_{index}",
                           "result_id": f"query_{index}", "status": "returned_mock",
                           "data": {"temperature_c": temperature, "sample_observed_at": index}}]}
    return policy.observe(task, "call_tools", args, result)


def test_third_identical_failure_pauses_once_despite_receipt_churn_and_restart(rig):
    store, task, policy = rig
    assert fail(policy, task, 1) is None
    assert fail(LoopPolicy(Store(store.path)), task, 2) is None
    result = fail(policy, task, 3)
    assert result["code"] == "NO_PROGRESS"
    assert result["attempts"] == 3
    assert result["classification"] == "parameters"
    assert result["status"] == "awaiting_input"
    assert store.task(task["id"])["status"] == "awaiting_input"
    assert fail(policy, task, 4) is None
    notices = [event for event in store.task_events(task["id"]) if event["kind"] == "task.question"]
    assert len(notices) == 1 and notices[0]["data"]["task_revision"] == 0


def test_unchanged_query_does_not_reset_failure_budget(rig):
    _, task, policy = rig
    query(policy, task, 20)
    for index in (1, 2):
        assert fail(policy, task, index) is None
        assert query(policy, task, 20, index) is None
    assert fail(policy, task, 3)["attempts"] == 3


def test_new_fact_resets_but_replaying_known_fact_does_not(rig):
    _, task, policy = rig
    query(policy, task, 20)
    fail(policy, task, 1)
    fail(policy, task, 2)
    query(policy, task, 21)
    assert fail(policy, task, 3) is None
    query(policy, task, 20, 4)
    assert fail(policy, task, 4) is None
    assert fail(policy, task, 5)["attempts"] == 3


def test_distinct_arguments_and_errors_are_not_identical_failures(rig):
    _, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    assert fail(policy, task, 3, temperature=46) is None
    assert fail(policy, task, 4, code="NO_MOCK_MATCH") is None
    assert fail(policy, task, 5)["attempts"] == 3


def test_changed_precondition_fact_resets_other_failure_counters(rig):
    _, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    args, result = failure(task, 3, code="PRECONDITION_NOT_MET")
    result["results"][0]["error"]["details"] = [
        {"pointer": "/vehicle/speed_kmh", "actual": 10, "expected": 0}]
    assert policy.observe(task, "call_tools", args, result) is None
    assert fail(policy, task, 4) is None
    assert fail(policy, task, 5) is None
    assert fail(policy, task, 6)["attempts"] == 3


@pytest.mark.parametrize("field", ["id", "timestamp", "device_id"])
def test_business_identifiers_and_time_arguments_remain_meaningful(rig, field):
    _, task, policy = rig
    for index, value in enumerate(["a", "b", "a", "a"]):
        args, result = failure(task, index)
        args["calls"][0]["arguments"][field] = value
        diagnostic = policy.observe(task, "call_tools", args, result)
        assert (diagnostic is not None) == (index == 3)


@pytest.mark.parametrize("name,field", [("search_capabilities", "matches"),
                                       ("browse_capabilities", "items")])
def test_repeated_empty_discovery_is_bounded(rig, name, field):
    store, task, policy = rig
    for index in range(3):
        result = policy.observe(task, name, {"query": "不存在", "path": []},
                                {field: [], "elapsed_ms": index, "request_id": str(index)})
        if index < 2:
            assert result is None
    assert result["classification"] == "empty_discovery"
    assert store.task(task["id"])["status"] == "awaiting_input"


def test_nested_error_without_status_and_known_schema_reads(rig):
    _, task, policy = rig
    schema = {"tools": [{"id": "control.climate.set_temperature", "version": "1",
                          "input_schema": {"type": "object"}}]}
    policy.observe(task, "describe_tools", {"tool_ids": ["control.climate.set_temperature"]}, schema)
    for index in range(3):
        args, _ = failure(task, index, temperature=23)
        result = policy.observe(task, "call_tools", args, {"results": [
            {"error": {"code": "OUTDATED_TARGET", "message": "最新目标是21度"}}]})
        if index < 2:
            assert result is None
            policy.observe(task, "describe_tools", {}, schema)
    assert result["classification"] == "parameters"


def test_nested_describe_error_is_counted(rig):
    _, task, policy = rig
    for _ in range(3):
        diagnostic = policy.observe(task, "describe_tools", {"tool_ids": ["missing"]},
                                    {"tools": [{"id": "missing", "error": {"code": "TOOL_NOT_FOUND"}}]})
    assert diagnostic["attempts"] == 3


@pytest.mark.parametrize("status", ["pending", "waiting", "awaiting_input", "awaiting_result"])
def test_pending_waits_neither_spend_nor_reset_budget(rig, status):
    store, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    for index in range(6):
        assert policy.observe(task, "wait_for_event", {}, {"status": status, "wait_id": str(index)}) is None
        args, result = failure(task, index)
        result["results"][0]["status"] = status
        assert policy.observe(task, "call_tools", args, result) is None
    assert store.task(task["id"])["status"] == "running"
    assert fail(policy, task, 3)["attempts"] == 3


def test_cached_preview_is_not_new_vehicle_state(rig):
    _, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    args, result = failure(task, 3)
    result["results"][0].update(status="prepared", error=None, data={"temperature_c": 23})
    assert policy.observe(task, "call_tools", args, result) is None
    assert fail(policy, task, 4)["attempts"] == 3


def revise(store, task):
    return store.revise_goal(task["id"], task["session_id"], "改成21", task["revision"],
                             f"change_{task['revision']}", "设置温度为21度", {})[0]


def test_goal_revision_task_isolation_and_late_old_results(rig):
    store, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    updated = revise(store, task)
    assert fail(policy, task, 3) is None
    args, result = failure(task, 4)
    assert policy.observe(updated, "call_tools", args, result) is None
    other, _ = store.create_task(task["session_id"], "other", "two")
    for current in (updated, other):
        assert fail(policy, current, 1) is None
        assert fail(policy, current, 2) is None
        assert store.task(current["id"])["status"] == "running"
    assert fail(policy, updated, 3)["task_revision"] == 1
    assert store.task(other["id"])["status"] == "running"


def test_revise_between_threshold_and_ask_cannot_pause_new_goal(rig, monkeypatch):
    store, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    ask = store.ask

    def racing_ask(tid, question, revision):
        revise(store, task)
        return ask(tid, question, revision)

    monkeypatch.setattr(store, "ask", racing_ask)
    assert fail(policy, task, 3) is None
    assert store.task(task["id"])["status"] == "running"
    assert not any(e["kind"] == "task.question" for e in store.task_events(task["id"]))


@pytest.mark.parametrize("end", ["cancel", "finish", "ask"])
def test_inactive_tasks_never_reopen_or_get_extra_questions(rig, end):
    store, task, policy = rig
    fail(policy, task, 1)
    fail(policy, task, 2)
    if end == "cancel":
        store.cancel(task["id"], task["session_id"])
    elif end == "finish":
        store.finish(task["id"], "failed", "结束")
    else:
        store.ask(task["id"], "已有问题")
    before = store.task_events(task["id"])
    assert fail(policy, task, 3) is None
    assert store.task_events(task["id"]) == before


def test_inactive_context_errors_and_missing_context_are_ignored(rig):
    store, task, policy = rig
    for _ in range(5):
        for code, message in [("TASK_INACTIVE", "stale"),
                              ("INVALID_CONTEXT", "TASK_INACTIVE：任务已暂停、取消或轮次过期")]:
            error = {"error": {"code": code, "message": message}}
            assert policy.observe(task, "browse_capabilities", {}, error) is None
        assert policy.observe(None, "browse_capabilities", {}, {"error": {"code": "INVALID_CONTEXT"}}) is None
    assert store.task(task["id"])["status"] == "running"


@pytest.mark.parametrize("code,category", [("NO_MOCK_MATCH", "backend_missing"),
    ("INTERFACE_UNAVAILABLE", "backend_missing"), ("INVALID_ARGUMENTS", "parameters"),
    ("INVALID_MOCK_OUTPUT", "backend_error"), ("NOT_EQUIPPED", "capability_unavailable"),
    ("PRECONDITION_NOT_MET", "precondition"), ("TIMEOUT", "transient")])
def test_failure_classification(rig, code, category):
    _, task, policy = rig
    for index in range(3):
        diagnostic = fail(policy, task, index, code=code)
    assert diagnostic["classification"] == category
    if category == "backend_missing":
        assert "不能据此断言车辆未配备" in diagnostic["instruction"]


def test_real_invalid_directory_error_counts_with_captured_context(rig):
    store, _, policy = rig
    catalog = CapabilityCatalog(store)
    service = CapabilityTools(store, catalog)
    task, _ = store.create_task(store.create_session(), "找目录", "directory",
                                catalog_revision=catalog.snapshot().revision, profile_id="demo-ready")
    args = {"task_id": task["id"], "task_revision": 0, "path": ["no_such_directory"]}
    captured, _ = service.context(task["id"], 0)
    for _ in range(3):
        result = service._call("browse_capabilities", args)
        assert result["error"]["code"] == "INVALID_CONTEXT"
        diagnostic = policy.observe(captured, "browse_capabilities", args, result)
    assert diagnostic["attempts"] == 3


def test_failed_item_cannot_hide_by_reordering_batch(rig):
    _, task, policy = rig
    for index in range(3):
        args, result = failure(task, index)
        query_call = {"call_id": f"query_{index}", "tool_id": "status.climate.get_state",
                      "tool_version": "1", "arguments": {}}
        args["calls"].append(query_call)
        result["results"].append({**query_call, "status": "returned_mock", "data": {"temperature_c": 20}})
        if index % 2:
            args["calls"].reverse()
            result["results"].reverse()
        diagnostic = policy.observe(task, "call_tools", args, result)
    assert diagnostic["attempts"] == 3


def test_concurrent_failure_observations_emit_only_one_pause(rig):
    store, task, policy = rig
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: fail(policy, task, index), range(8)))
    assert sum(result is not None for result in results) == 1
    assert sum(event["kind"] == "task.question" for event in store.task_events(task["id"])) == 1
