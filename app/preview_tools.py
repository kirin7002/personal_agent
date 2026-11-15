from .catalog import Catalog
from .store import Store


class PreviewTools:
    def __init__(self, store: Store, catalog: Catalog):
        self.store, self.catalog = store, catalog

    def call(self, name: str, arguments: dict):
        arguments = dict(arguments)
        tid = arguments.pop("task_id", None)
        revision = arguments.pop("task_revision", None)
        if not tid:
            return {"error": "missing_task_id", "message": "必须关联当前任务", "executed": False}
        try:
            task = self.store.task(tid)
            if task["status"] != "running" or type(revision) is not int or task["revision"] != revision:
                return {"error": "task_inactive", "message": "任务已暂停或结束", "executed": False}
            if name == "request_user_input":
                question = arguments.get("question", "")
                if not isinstance(question, str) or not question.strip() or len(question) > 500 or set(arguments) != {"question"}:
                    return {"error": "invalid_question", "executed": False}
                return self.store.ask(tid, question.strip(), revision)
            errors = self.catalog.validate(name, arguments)
            if errors:
                self.store.event(tid, "tool.invalid", {"tool": name, "errors": errors})
                return {"error": "schema_validation_failed", "errors": errors, "executed": False}
            return self.store.record_call(tid, name, arguments, revision)
        except (KeyError, ValueError) as exc:
            return {"error": "invalid_task", "message": str(exc), "executed": False}
