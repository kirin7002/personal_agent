import json
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from .config import ROOT


class Catalog:
    def __init__(self, directory: Path | None = None):
        directory = directory or ROOT / "catalog"
        self.document = json.loads((directory / "vehicle-tools.json").read_text())
        self.output_schema = json.loads((directory / "output.schema.json").read_text())
        self.tools = {tool["name"]: tool for tool in self.document["tools"]}
        if len(self.tools) != len(self.document["tools"]):
            raise ValueError("工具名称不能重复")
        Draft202012Validator.check_schema(self.output_schema)
        for tool in self.tools.values():
            Draft202012Validator.check_schema(tool["parameters"])
            errors = self.validate(tool["name"], tool["example"]["arguments"])
            if errors:
                raise ValueError(f"工具示例不符合 Schema: {tool['name']}")

    def validate(self, name: str, arguments: dict) -> list[dict]:
        if name not in self.tools:
            return [{"path": "tool", "message": "未知工具，不允许生成未注册的控制能力"}]
        errors = Draft202012Validator(self.tools[name]["parameters"]).iter_errors(arguments)
        return [{"path": ".".join(map(str, e.path)) or "arguments", "message": e.message} for e in errors]

    def output(self, calls: list[dict]) -> dict:
        result = {"schema_version": self.document["schema_version"], "mode": "dry_run", "executed": False, "calls": calls}
        Draft202012Validator(self.output_schema).validate(result)
        for call in calls:
            if self.validate(call["tool"], call["arguments"]):
                raise ValueError("输出参数没有通过工具 Schema 校验")
        return result

    def mcp_tools(self) -> list[dict]:
        result = []
        for tool in self.tools.values():
            schema = deepcopy(tool["parameters"])
            schema["properties"]["task_id"] = {"type": "string", "description": "当前任务的 task_id，必须使用任务提供的值"}
            schema["properties"]["task_revision"] = {"type": "integer", "description": "当前任务轮次，使用任务提供的 task_revision"}
            schema["required"] = ["task_id", "task_revision", *schema.get("required", [])]
            result.append({"name": tool["name"], "description": tool["description"], "inputSchema": schema})
        result.append({"name": "request_user_input", "description": "缺少必要参数或目标不明确时向用户追问。调用后结束本轮，等待用户回答，不猜测关键参数。", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "task_revision": {"type": "integer"}, "question": {"type": "string", "minLength": 1, "maxLength": 500}}, "required": ["task_id", "task_revision", "question"], "additionalProperties": False}})
        return result
