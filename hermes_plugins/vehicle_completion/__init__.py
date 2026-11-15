"""Hermes execution middleware: locally close validated tasks without another provider call.

Only the installed Anthropic transport is supported. Other providers keep the normal loop.
No prompt text or model reasoning is logged. The task database is the completion authority.
"""
from contextlib import suppress
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from app.direct_completion import request_digest
from app.store import Store


def task_context(request, store):
    # Read the structured task envelope, never search inside the user's goal text.
    for message in reversed(request.get('messages', [])):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        parts = [content] if isinstance(content, str) else [p.get('text') for p in content or [] if isinstance(p, dict) and p.get('type') == 'text']
        for text in parts:
            if not isinstance(text, str):
                continue
            with suppress(ValueError, KeyError, TypeError):
                data = json.loads(text)
                task = store.task(data['task_id'])
                # A registered change preserves the original structured envelope.
                # Bind only a historically registered goal/revision, never arbitrary text.
                with store.connect() as db:
                    registered = db.execute('SELECT 1 FROM task_changes WHERE task_id=? AND previous_revision=? AND original_goal=?',
                                            (task['id'], data.get('task_revision'), data.get('user_request'))).fetchone()
                if ((registered or (data['user_request'] == task['goal'] and data['task_revision'] == task['revision'])) and data['catalog_revision'] == task['catalog_revision']
                        and task['source'] == 'hermes'):
                    return task
    return None


def local_response(message, model):
    # Provider-shaped local final response is the documented llm_execution replacement
    # contract. Zero usage + local_ id distinguish it from an actual provider response.
    return SimpleNamespace(id='local_vehicle_completion', type='message', role='assistant', model=model,
                           content=[SimpleNamespace(type='text', text=message)], stop_reason='end_turn', stop_sequence=None,
                           usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def execute(request, next_call, api_mode='', api_request_id='', **kwargs):
    path = os.getenv('PREVIEW_DB', '')
    if api_mode != 'anthropic_messages' or not path or not Path(path).is_file():
        return next_call(request)
    store = Store(Path(path))
    task = task_context(request, store)
    if task is None:
        return next_call(request)
    if task['status'] in {'awaiting_input','waiting','cancelled','expired'}:
        # A question or wait has already been recorded by the application.
        # Do not spend another provider round merely to close this native run.
        with suppress(sqlite3.Error):
            store.event(task['id'],'agent.wait_confirmation_skipped',{'task_revision':task['revision'],'status':task['status']})
        return local_response('本轮处理已停止，任务状态由应用记录。',request.get('model',''))
    events = store.task_events(task['id'])
    classified = any(e['kind'] == 'task.outcome_resolved' and e['data'].get('source') == 'validated_receipts' for e in events)
    if task['status'] in {'generated','completed','unsupported','partial','failed'} and task.get('final_result'):
        with suppress(sqlite3.Error):
            store.event(task['id'], 'agent.confirmation_skipped', {'source': 'validated_receipts' if classified else 'local_validator', 'task_revision': task['revision']})
        return local_response(task['summary'], request.get('model', ''))
    store.event(task['id'], 'agent.model_request', {'api_request_id': api_request_id, 'task_revision': task['revision']})
    response = next_call(request)
    # Authorize only a single call_tools operation from the FIRST model response.
    # Parallel or multi-tool responses can contain unfinished work, so never opt in.
    blocks = [b for b in getattr(response, 'content', []) if getattr(b, 'type', '') == 'tool_use']
    if len(blocks) == 1 and blocks[0].name == 'mcp__vehicle_preview__call_tools':
        args = blocks[0].input
        if isinstance(args, dict) and args.get('complete_if_success') is True and args.get('task_id') == task['id'] and args.get('task_revision') == task['revision']:
            with suppress(sqlite3.Error, ValueError):
                store.event(task['id'], 'agent.single_batch', {'digest': request_digest(args), 'task_revision': task['revision'], 'api_request_id': api_request_id})
    return response


def register(ctx):
    ctx.register_middleware('llm_execution', execute)
