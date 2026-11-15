"""Instrument the provider boundary inside the production completion plugin.

No synthetic completion or task-context binding is supplied by this experiment.
The production plugin alone decides whether to call or bypass the provider.
"""
import json
import importlib.util
import os
from pathlib import Path

from app.store import Store
from .barriers import mark, state, wait_release

# Hermes owns a synthetic `hermes_plugins` package in the gateway process.
# Load the unchanged production source by filename to avoid that namespace.
_spec = importlib.util.spec_from_file_location(
    "running_steer_production_completion", Path(__file__).resolve().parents[2]
    / "hermes_plugins/vehicle_completion/__init__.py")
_production = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_production)
production_execute = _production.execute


def audit(data):
    path = os.environ.get("RUNNING_STEER_AUDIT")
    if path:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(data) + "\n")


def registered_task(request, store):
    # Used only to bind timing telemetry, never to authorize a task or completion.
    for message in request.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content", [])
        parts = [content] if isinstance(content, str) else [
            block.get("text", "") for block in content if isinstance(block, dict)
        ]
        for part in parts:
            try:
                tid = json.loads(part)["task_id"]
                if state(store, tid):
                    return tid
            except (ValueError, TypeError, KeyError):
                pass
    return None


def execute(request, next_call, **kwargs):
    store = Store(Path(os.environ["PREVIEW_DB"]))
    tid = registered_task(request, store)
    audit({"stage": "execute", "api_mode": kwargs.get("api_mode"), "bound": bool(tid),
           "roles": [m.get("role") for m in request.get("messages", [])]})

    def observed_provider(provider_request):
        row = state(store, tid) if tid else None
        first = row and row["provider_started"] is None
        if first:
            mark(store, tid, "provider_started")
            if row["window"] == "during_model":
                mark(store, tid, "reached")
        # This is a real provider invocation. Its return timestamp is recorded
        # before any response hold, so a fast response cannot fake in-flight proof.
        try:
            response = next_call(provider_request)
        finally:
            if first:
                mark(store, tid, "provider_returned")
        if first and row["window"] == "during_model":
            wait_release(store, tid)
        return response

    return production_execute(request, observed_provider, **kwargs)


def register(ctx):
    audit({"stage": "register"})
    ctx.register_middleware("llm_execution", execute)
