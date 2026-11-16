from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import shutil

from jsonschema import Draft202012Validator
import pytest

from app.capability_catalog import CapabilityCatalog, CatalogError
from app.capability_tools import CapabilityTools, entrypoint_schemas
from app.config import ROOT, Settings
from app.hermes import TaskManager
from app.store import Store


@pytest.fixture
def caps(tmp_path):
    source = tmp_path / "capabilities"
    shutil.copytree(ROOT / "capabilities", source)
    store = Store(tmp_path / "test.db")
    catalog = CapabilityCatalog(store, source)
    sid = store.create_session()
    return store, catalog, sid, CapabilityTools(store, catalog)


def new_task(caps, profile="demo-ready"):
    store, catalog, sid, service = caps
    import uuid
    task, _ = store.create_task(sid, "测试目标", uuid.uuid4().hex, catalog_revision=catalog.snapshot().revision, profile_id=profile)
    return task, {"task_id": task["id"], "task_revision": task["revision"]}


def call_tool(caps, ctx, tool_id, args, call_id="one"):
    store, catalog, sid, service = caps
    snap = catalog.snapshot(store.task(ctx["task_id"])["catalog_revision"])
    tool = snap.tools[tool_id]
    return service.call("call_tools", {**ctx, "catalog_revision": snap.revision, "calls": [{"call_id": call_id, "tool_id": tool_id, "tool_version": tool["version"], "arguments": args}]})["results"][0]


CONTROL_CASES = json.loads((ROOT / "capabilities/scenarios/control-50.json").read_text())


@pytest.mark.parametrize("scenario", CONTROL_CASES, ids=[s["id"] for s in CONTROL_CASES])
def test_all_50_controls_generate_valid_previews(caps, scenario):
    store, catalog, sid, service = caps
    task, ctx = new_task(caps)
    results = [call_tool(caps, ctx, c["tool_id"], c["arguments"], f"step-{i}") for i, c in enumerate(scenario["expected"]["calls"])]
    assert all(r["status"] == "prepared" and r["executed"] is False for r in results)
    finish = service.call("finish_task", {**ctx, "outcome": "prepared", "summary": "测试完成", "result_ids": [r["result_id"] for r in results]})
    assert finish["status"] == "generated"
    final = json.loads(store.task(task["id"])["final_result"])
    assert [{"tool_id": c["tool"], "arguments": c["arguments"]} for c in final["calls"]] == scenario["expected"]["calls"]
    assert catalog.snapshot().profiles["demo-ready"]["data"]["state"]["climate"]["driver_temperature_c"] == 22


def test_query_tools_all_profiles_and_answer_semantics(caps):
    store, catalog, sid, service = caps
    snap = catalog.snapshot()
    assert len(snap.tools) == 68
    assert len([t for t in snap.tools.values() if t["kind"] == "manual"]) == 8
    assert len([t for t in snap.tools.values() if t["kind"] == "status"]) == 10
    for profile in snap.profiles:
        for tool in snap.tools.values():
            if tool["kind"] == "control":
                continue
            task, ctx = new_task(caps, profile)
            result = call_tool(caps, ctx, tool["id"], tool["examples"][0]["arguments"])
            assert result["status"] == "returned_mock"
            assert result["simulated"] and not result["executed"]
            Draft202012Validator(tool["output_schema"]).validate(result["data"])
            reply = service.call("finish_task", {**ctx, "outcome": "answered", "summary": "查询结果来自模拟快照", "result_ids": [result["result_id"]]})
            assert reply["status"] == "completed"
            assert "下发成功" not in reply["message"]


def test_directory_browse_and_lazy_schema(caps):
    store, catalog, sid, service = caps
    task, ctx = new_task(caps)
    root = service.call("browse_capabilities", ctx)
    assert [x["path"] for x in root["items"]] == [["control"], ["manual"], ["status"]]
    assert "input_schema" not in json.dumps(root)
    control = service.call("browse_capabilities", {**ctx, "path": ["control"]})
    assert len(control["items"]) == 8
    climate = service.call("browse_capabilities", {**ctx, "path": ["control", "climate"], "query": "风向"})
    assert climate["items"][0]["id"] == "control.climate.set_airflow"
    details = service.call("describe_tools", {**ctx, "tool_ids": ["control.climate.set_airflow", "missing.tool"]})
    assert "input_schema" in details["tools"][0] and "mock" not in details["tools"][0]
    assert details["tools"][1]["error"]["code"] == "TOOL_NOT_FOUND"
    assert len(entrypoint_schemas()) == 7


@pytest.mark.parametrize("profile,code", [("demo-no-suspension", "NOT_EQUIPPED"), ("demo-interface-missing", "INTERFACE_UNAVAILABLE"), ("demo-driving", "PRECONDITION_NOT_MET")])
def test_cross_domain_evidence(caps, profile, code):
    store, catalog, sid, service = caps
    task, ctx = new_task(caps, profile)
    failed = call_tool(caps, ctx, "control.suspension.set_height", {"action": "raise"})
    assert failed["error"]["code"] == code
    configuration = call_tool(caps, ctx, "status.vehicle.get_configuration", {}, "config")
    manual = call_tool(caps, ctx, "manual.suspension.lookup", {"topic": "overview"}, "manual")
    result = service.call("finish_task", {**ctx, "outcome": "unsupported", "summary": "模拟条件不满足", "result_ids": [r["result_id"] for r in [failed, configuration, manual]]})
    assert result["status"] == "unsupported"
    assert not store.task(task["id"])["calls"]


def test_idempotency_conflict_cancellation_and_revision(caps):
    store, catalog, sid, service = caps
    task, ctx = new_task(caps)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call_tool(caps, ctx, "control.multimedia.set_volume", {"volume_percent": 30}), range(2)))
    assert results[0]["result_id"] == results[1]["result_id"]
    assert len(store.capability_results(task["id"])) == 1
    assert call_tool(caps, ctx, "control.multimedia.set_volume", {"volume_percent": 40})["error"]["code"] == "CALL_ID_CONFLICT"
    assert service.call("request_user_input", {**ctx, "question": "请补充目标"})["status"] == "awaiting_input"
    resumed = store.resume(task["id"], sid, "继续")
    assert service.call("browse_capabilities", ctx)["error"]
    store.cancel(task["id"], sid)
    assert service.call("browse_capabilities", {"task_id": task["id"], "task_revision": resumed["revision"]})["error"]


def test_finish_requires_owned_evidence_and_outcome_matches(caps):
    store, catalog, sid, service = caps
    task, ctx = new_task(caps)
    assert service.call("finish_task", {**ctx, "outcome": "prepared", "summary": "成功", "result_ids": []})["error"]
    assert service.call("finish_task", {**ctx, "outcome": "answered", "summary": "成功", "result_ids": ["invented"]})["error"]
    receipt = call_tool(caps, ctx, "status.climate.get_state", {})
    assert service.call("finish_task", {**ctx, "outcome": "prepared", "summary": "成功", "result_ids": [receipt["result_id"]]})["error"]
    other, otherctx = new_task(caps)
    assert service.call("finish_task", {**otherctx, "outcome": "answered", "summary": "成功", "result_ids": [receipt["result_id"]]})["error"]["code"] == "INVALID_EVIDENCE"


def test_business_schema_rejects_invalid_arguments_and_allows_explicit_correction(caps):
    store, catalog, sid, service = caps
    task, ctx = new_task(caps)
    bad = call_tool(caps, ctx, "control.climate.set_temperature", {"zone": "all", "temperature_c": 45})
    assert bad["error"]["code"] == "INVALID_ARGUMENTS"
    assert store.task(task["id"])["calls"] == []
    corrected = call_tool(caps, ctx, "control.climate.set_temperature", {"zone": "all", "temperature_c": 23}, "corrected")
    assert corrected["status"] == "prepared"
    invalid_finish = service.call("finish_task", {**ctx, "outcome": "prepared", "summary": "完成", "result_ids": [bad["result_id"], corrected["result_id"]]})
    assert invalid_finish["error"]["code"] == "INVALID_OUTCOME"
    partial = service.call("finish_task", {**ctx, "outcome": "partial", "summary": "部分生成", "result_ids": [bad["result_id"], corrected["result_id"]], "unresolved": ["原45度要求不在模拟范围内"]})
    assert partial["status"] == "partial" and "全部成功" not in partial["message"]


def test_nested_category_and_stale_import_preview(caps):
    store, catalog, sid, service = caps
    bundle = deepcopy(catalog.snapshot().bundle)
    bundle["directories"].append({"path": ["control", "climate", "front"], "title": "前排", "description": "更深一层目录", "aliases": ["前排"]})
    tool = deepcopy(bundle["tools"][0])
    tool["id"] = "control.climate.front.extra"
    tool["category_path"] = ["control", "climate", "front"]
    bundle["tools"].append(tool)
    first = catalog.preview_import(bundle)
    second = catalog.preview_import(bundle)
    catalog.apply_import(first["preview_id"], first["base_revision"])
    with pytest.raises(CatalogError, match="变化"):
        catalog.apply_import(second["preview_id"], second["base_revision"])
    assert catalog.snapshot().browse(["control", "climate", "front"])["items"][0]["id"] == tool["id"]


def test_import_export_and_add_file_without_prompt_changes(caps):
    store, catalog, sid, service = caps
    old_task, ctx = new_task(caps)
    original = catalog.snapshot()
    baseline_prompt = TaskManager(store, catalog, Settings(hermes_key="")).instructions()
    preview = catalog.preview_import(deepcopy(original.bundle))
    assert preview["new_revision"] == original.revision
    assert catalog.apply_import(preview["preview_id"], original.revision).revision == original.revision
    tool = deepcopy(original.tools["control.climate.set_temperature"])
    tool["id"] = "control.climate.set_demo_target"
    tool["title"] = "新增测试温度工具"
    destination = catalog.root / "tools/control/climate/new-tool.json"
    destination.write_text(json.dumps(tool))
    newer = catalog.reload()
    assert newer.revision != original.revision
    assert len(entrypoint_schemas()) == 7
    assert TaskManager(store, catalog, Settings(hermes_key="")).instructions() == baseline_prompt
    # Old tasks stay pinned, including after clarification/resume.
    assert service.call("describe_tools", {**ctx, "tool_ids": [tool["id"]]})["tools"][0]["error"]
    newtask, newctx = new_task(caps)
    result = call_tool(caps, newctx, tool["id"], {"zone": "all", "temperature_c": 24})
    assert result["status"] == "prepared"
    assert catalog.validate(newer.bundle).revision == newer.revision


def test_atomic_import_validation_versioning_and_persistence(caps):
    store, catalog, sid, service = caps
    before = catalog.snapshot()
    tool = deepcopy(before.tools["control.climate.set_temperature"])
    tool["input_schema"]["properties"]["temperature_c"]["maximum"] = 31
    with pytest.raises(CatalogError, match="version"):
        catalog.preview_import(tool, "upsert")
    assert catalog.snapshot().revision == before.revision
    previous_version=tool["version"]
    pieces=[int(v) for v in previous_version.split(".")]
    pieces[-1]+=1
    tool["version"]=".".join(map(str,pieces))
    preview = catalog.preview_import(tool, "upsert")
    new = catalog.apply_import(preview["preview_id"], preview["base_revision"])
    assert catalog.load_files().revision == new.revision
    assert catalog.snapshot(before.revision).tools[tool["id"]]["version"] == previous_version
    assert list((store.path.parent / "catalog-backups").iterdir())
    bad = deepcopy(new.bundle)
    bad["tools"][0]["input_schema"] = {"type": "object", "$ref": "https://example.com/schema"}
    with pytest.raises(CatalogError):
        catalog.preview_import(bad, "replace")
    assert catalog.snapshot().revision == new.revision
    bad = deepcopy(new.bundle)
    bad["tools"][0]["category_path"] = ["control", "missing"]
    with pytest.raises(CatalogError):
        catalog.preview_import(bad, "replace")


def test_mock_output_validation_and_missing_reference(caps):
    store, catalog, sid, service = caps
    bundle = deepcopy(catalog.snapshot().bundle)
    manual = next(t for t in bundle["tools"] if t["kind"] == "manual")
    manual["mock"]["cases"]["overview"] = {"unexpected": "bad"}
    with pytest.raises(CatalogError):
        catalog.validate(bundle)
    bundle = deepcopy(catalog.snapshot().bundle)
    bundle["directories"].append(deepcopy(bundle["directories"][0]))
    with pytest.raises(CatalogError, match="重复"):
        catalog.validate(bundle)
