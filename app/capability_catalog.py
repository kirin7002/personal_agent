"""Declarative catalog loading, immutable snapshots and atomic configuration import."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

from jsonschema import Draft202012Validator

from .config import ROOT
from .store import Store

FORMAT = "capability-catalog/v1"


class CatalogError(ValueError):
    pass


def pointer(data, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise CatalogError("JSON Pointer 必须以 / 开头")
    value = data
    try:
        for segment in path[1:].split("/"):
            key = segment.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not key.isdecimal():
                    raise KeyError(key)
                value = value[int(key)]
            else:
                value = value[key]
        return deepcopy(value)
    except (KeyError, IndexError, TypeError):
        raise CatalogError(f"模拟数据不存在路径 {path}") from None


def json_read(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise CatalogError("单个配置文件不能超过 4 MiB")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, UnicodeError):
        raise CatalogError(f"JSON 无效：{path.name}") from None


def canonical(bundle):
    result = deepcopy(bundle)
    for key in ["tools", "profiles", "scenarios"]:
        result[key] = sorted(result[key], key=lambda x: x["id"])
    result["directories"] = sorted(result["directories"], key=lambda x: x["path"])
    return result


def revision_of(bundle):
    raw = json.dumps(canonical(bundle), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "cat_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def validate_schema(schema):
    def walk(value):
        if isinstance(value, dict):
            for ref_key in ["$ref", "$dynamicRef", "$recursiveRef"]:
                ref = value.get(ref_key)
                if ref is not None and (not isinstance(ref, str) or not ref.startswith("#")):
                    raise CatalogError("工具 Schema 只允许同文档内部引用")
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise CatalogError(f"工具 Schema 无效：{getattr(exc, 'message', type(exc).__name__)}") from None


def mock_data(tool, arguments, profile):
    mock = tool["mock"]
    mode = mock["mode"]
    if mode == "prepare_only":
        return {"prepared": True}
    if mode == "static":
        return deepcopy(mock["data"])
    if mode == "profile_pointer":
        return pointer(profile["data"], mock["pointer"])
    value = arguments.get(mock["argument"])
    if not isinstance(value, str) or value not in mock["cases"]:
        raise CatalogError("NO_MOCK_MATCH：此参数没有对应模拟数据")
    return deepcopy(mock["cases"][value])


class CatalogSnapshot:
    def __init__(self, bundle):
        self.bundle = canonical(bundle)
        self.revision = revision_of(bundle)
        self.tools = {t["id"]: t for t in self.bundle["tools"]}
        self.directories = {tuple(d["path"]): d for d in self.bundle["directories"]}
        self.profiles = {p["id"]: p for p in self.bundle["profiles"]}
        self.scenarios = {s["id"]: s for s in self.bundle["scenarios"]}

    def browse(self, path=(), query="", offset=0, limit=30):
        path = tuple(path)
        if path and path not in self.directories:
            raise CatalogError("DIRECTORY_NOT_FOUND：目录路径不存在")
        if not 1 <= limit <= 50 or offset < 0:
            raise CatalogError("分页参数不合法")
        items = []
        for child, d in self.directories.items():
            if child[:-1] == path:
                items.append({"node_type": "directory", **deepcopy(d)})
        for tool in self.tools.values():
            if tuple(tool["category_path"]) == path and tool["enabled"]:
                items.append({"node_type": "tool", **{k: deepcopy(tool[k]) for k in ["id", "version", "title", "description", "kind", "aliases"]}})
        if query:
            # Deliberately deterministic, CJK-preserving substring filtering; not semantic search.
            q = query.casefold().strip()
            items = [x for x in items if q in json.dumps(x, ensure_ascii=False).casefold()]
        total = len(items)
        return {"catalog_revision": self.revision, "path": list(path), "items": items[offset:offset+limit], "total": total, "next_offset": offset+limit if offset+limit < total else None}

    def describe(self, ids):
        described = []
        for tid in ids:
            tool = self.tools.get(tid)
            if tool is None or not tool["enabled"]:
                described.append({"id": tid, "error": {"code": "TOOL_NOT_FOUND" if tool is None else "TOOL_DISABLED"}})
            else:
                described.append({k: deepcopy(v) for k, v in tool.items() if k not in {"mock", "enabled"}})
        return {"catalog_revision": self.revision, "tools": described}


class CapabilityCatalog:
    def __init__(self, store: Store, root=None, initialize=True):
        self.store = store
        self.root = Path(root or ROOT / "capabilities").resolve()
        self.schema = json_read(self.root / "schemas/bundle.schema.json")
        self.cache = {}
        self.staged = {}
        self.lock = threading.RLock()
        if initialize or not store.active_catalog_revision():
            self.reload()

    def load_files(self):
        manifest = json_read(self.root / "manifest.json")
        bundle = {**manifest, "directories": [], "tools": [], "profiles": [], "scenarios": []}
        for key in ["directories", "tools", "profiles", "scenarios"]:
            for path in sorted((self.root / key).rglob("*.json")):
                if not path.resolve().is_relative_to(self.root):
                    raise CatalogError("配置文件不能指向目录外部")
                data = json_read(path)
                if key == "scenarios":
                    if not isinstance(data, list):
                        raise CatalogError("场景配置文件必须是数组")
                    bundle[key].extend(data)
                else:
                    bundle[key].append(data)
        return self.validate(bundle)

    def validate(self, bundle):
        errors = list(Draft202012Validator(self.schema).iter_errors(bundle))
        if errors:
            raise CatalogError("配置协议校验失败：" + "; ".join(f"{'/'.join(map(str,e.path))}: {e.message}" for e in errors[:8]))
        if len(bundle["tools"]) > 2000 or len(bundle["directories"]) > 1000:
            raise CatalogError("目录超过本 Demo 的容量上限")
        for key in ["tools", "profiles", "scenarios"]:
            ids = [x["id"] for x in bundle[key]]
            if len(ids) != len(set(ids)):
                raise CatalogError(f"{key} 存在重复 ID")
        paths = [tuple(d["path"]) for d in bundle["directories"]]
        if len(paths) != len(set(paths)):
            raise CatalogError("目录路径重复")
        for path in paths:
            if len(path) > 1 and path[:-1] not in paths:
                raise CatalogError(f"目录缺少父节点：{'.'.join(path)}")
        if set(p[0] for p in paths) != {"control", "manual", "status"}:
            raise CatalogError("顶层目录必须为 control、manual、status")
        snap = CatalogSnapshot(bundle)
        for directory in snap.directories.values():
            for related in directory.get("related_query_paths", []):
                if tuple(related) not in snap.directories or related[0] not in {"manual", "status"}:
                    raise CatalogError("关联查询目录必须指向存在的车书或车况目录")
        for tool in snap.tools.values():
            if tuple(tool["category_path"]) not in snap.directories or tool["category_path"][0] != tool["kind"]:
                raise CatalogError(f"工具分类无效：{tool['id']}")
            for name in ["input_schema", "output_schema"]:
                validate_schema(tool[name])
            if tool["input_schema"].get("type") != "object":
                raise CatalogError("工具参数的根类型必须是 object")
            if (tool["kind"] == "control") != (tool["mock"]["mode"] == "prepare_only"):
                raise CatalogError("车控仅允许 prepare_only，查询工具不得使用 prepare_only")
            for example in tool["examples"]:
                problem = next(Draft202012Validator(tool["input_schema"]).iter_errors(example["arguments"]), None)
                if problem:
                    raise CatalogError(f"工具示例参数无效：{tool['id']}：{problem.message}")
                for profile in snap.profiles.values():
                    data = mock_data(tool, example["arguments"], profile)
                    problem = next(Draft202012Validator(tool["output_schema"]).iter_errors(data), None)
                    if problem:
                        raise CatalogError(f"模拟结果无效：{tool['id']} / {profile['id']}：{problem.message}")
            if tool["mock"]["mode"] == "argument_lookup":
                for data in tool["mock"]["cases"].values():
                    if not Draft202012Validator(tool["output_schema"]).is_valid(data):
                        raise CatalogError(f"模拟 lookup 输出无效：{tool['id']}")
            for condition in tool.get("preconditions", []):
                for profile in snap.profiles.values():
                    pointer(profile["data"], condition["pointer"])
        for scenario in snap.scenarios.values():
            if scenario["profile_id"] not in snap.profiles:
                raise CatalogError(f"场景引用未知 profile：{scenario['id']}")
            for call in scenario["expected"]["calls"]:
                tool = snap.tools.get(call["tool_id"])
                if tool is None or not Draft202012Validator(tool["input_schema"]).is_valid(call["arguments"]):
                    raise CatalogError(f"场景预期调用无效：{scenario['id']}")
        return snap

    def snapshot(self, revision=None):
        revision = revision or self.store.active_catalog_revision()
        if revision not in self.cache:
            self.cache[revision] = CatalogSnapshot(self.store.catalog_payload(revision))
        return self.cache[revision]

    def publish(self, snapshot):
        self.store.publish_catalog(snapshot.revision, snapshot.bundle)
        self.cache[snapshot.revision] = snapshot
        return snapshot

    def reload(self):
        with self.lock:
            new = self.load_files()
            previous = self.store.active_catalog_revision()
            if previous:
                self.check_tool_versions(self.snapshot(previous), new)
            return self.publish(new)

    @staticmethod
    def check_tool_versions(before, after):
        for tid, tool in after.tools.items():
            previous = before.tools.get(tid)
            if previous and previous != tool and tuple(map(int, tool["version"].split("."))) <= tuple(map(int, previous["version"].split("."))):
                raise CatalogError(f"工具更新必须递增 version：{tid}")

    @staticmethod
    def diff(before, after):
        result = {}
        for key in ["directories", "tools", "profiles", "scenarios"]:
            index = lambda item: ".".join(item["path"]) if key == "directories" else item["id"]
            old = {index(x): x for x in before[key]}
            new = {index(x): x for x in after[key]}
            result[key] = {"added": sorted(new.keys()-old.keys()), "removed": sorted(old.keys()-new.keys()), "modified": sorted(k for k in old.keys() & new.keys() if old[k] != new[k])}
        return result

    def preview_import(self, payload, mode="merge"):
        if mode not in {"merge", "upsert", "replace"}:
            raise CatalogError("未知导入模式")
        before = self.snapshot()
        partial = isinstance(payload, dict) and "id" in payload and "kind" in payload
        if partial:
            if mode == "replace":
                raise CatalogError("单工具文件不能替换整份目录")
            incoming = {**deepcopy(before.bundle), "tools": [payload], "directories": [], "profiles": [], "scenarios": []}
        else:
            incoming = deepcopy(payload)
            if not isinstance(incoming, dict) or incoming.get("format_version") != FORMAT:
                raise CatalogError("未知目录格式版本")
            # Full bundles must stand alone and be valid before a merge.
            self.validate(incoming)
        if mode == "replace":
            merged = incoming
        else:
            merged = deepcopy(before.bundle)
            for key in ["directories", "tools", "profiles", "scenarios"]:
                index = lambda item: ".".join(item["path"]) if key == "directories" else item["id"]
                existing = {index(x): x for x in merged[key]}
                for item in incoming[key]:
                    ident = index(item)
                    if ident in existing and item != existing[ident] and mode == "merge":
                        raise CatalogError(f"{ident} 已存在且内容不同；请选择 upsert 或 replace")
                    existing[ident] = item
                merged[key] = list(existing.values())
            if not partial:
                merged["package"] = incoming["package"]
        new = self.validate(merged)
        self.check_tool_versions(before, new)
        token = uuid.uuid4().hex
        self.staged = {k: v for k, v in self.staged.items() if time.monotonic()-v[0] < 600}
        if len(self.staged) >= 20:
            self.staged.pop(next(iter(self.staged)))
        self.staged[token] = (time.monotonic(), before.revision, new)
        return {"preview_id": token, "base_revision": before.revision, "new_revision": new.revision, "diff": self.diff(before.bundle, new.bundle), "counts": {k: len(new.bundle[k]) for k in ["directories", "tools", "profiles", "scenarios"]}}

    def apply_import(self, preview_id, base_revision):
        with self.lock:
            staged = self.staged.get(preview_id)
            if not staged or time.monotonic()-staged[0] > 600:
                raise CatalogError("导入预览已过期，请重新预览")
            _, original, new = staged
            if base_revision != original or self.store.active_catalog_revision() != original:
                raise CatalogError("目录已发生变化，请重新预览导入")
            if self.load_files().revision != original:
                raise CatalogError("配置文件有未加载的修改，请先重新加载")
            if new.revision == original:
                self.staged.pop(preview_id, None)
                return new
            self.persist(new)
            self.staged.pop(preview_id, None)
            return new

    def persist(self, snapshot):
        staging = Path(tempfile.mkdtemp(prefix=".capabilities-stage-", dir=self.root.parent))
        backup = self.root.with_name(".capabilities-backup-"+uuid.uuid4().hex)
        def save(relative, data):
            p = staging / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
        moved = False
        try:
            shutil.copytree(self.root / "schemas", staging / "schemas")
            b = snapshot.bundle
            save("manifest.json", {k: b[k] for k in ["format_version", "package"]})
            for d in b["directories"]:
                save("directories/"+".".join(d["path"])+".json", d)
            for tool in b["tools"]:
                save("tools/"+"/".join(tool["category_path"])+"/"+tool["id"]+".json", tool)
            for profile in b["profiles"]:
                # Profiles have user-defined labels; filenames use a hash, never raw identifiers.
                save("profiles/"+hashlib.sha256(profile["id"].encode()).hexdigest()[:20]+".json", profile)
            save("scenarios/all.json", b["scenarios"])
            save("exports/vehicle-capabilities.bundle.json", b)
            os.replace(self.root, backup)
            moved = True
            os.replace(staging, self.root)
            self.publish(snapshot)
        except BaseException:
            if moved:
                if self.root.exists():
                    shutil.rmtree(self.root)
                os.replace(backup, self.root)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        # Keep the exact previous source files for recovery, outside the active loader.
        backups = self.store.path.parent / "catalog-backups"
        backups.mkdir(exist_ok=True)
        os.replace(backup, backups / backup.name)
