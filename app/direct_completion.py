"""Conservative completion gates shared by the task service and Hermes middleware."""
import hashlib
import json

from .tool_preload import complex_request


def request_digest(arguments):
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def eligible(task, preload):
    assessment = preload.get('control_assessment')
    if assessment is not None and not assessment['single_call_complete']:
        return False
    return (task.get('completion_mode') == 'auto' and task.get('discovery_mode') == 'auto'
            and task['source'] == 'hermes' and task['revision'] == 0 and not task['history']
            and not complex_request(task['goal']) and len(preload.get('tool_ids', [])) == 1
            and len(preload.get('scores', [])) == 1 and preload['scores'][0] >= .82)


def can_complete(task, events, records, arguments):
    if arguments.get('complete_if_success') is not True or len(arguments.get('calls', [])) != 1:
        return False
    preloads = [e['data'] for e in events if e['kind'] == 'catalog.preloaded']
    if len(preloads) != 1 or not eligible(task, preloads[0]):
        return False
    blocked = {'catalog.browsed', 'catalog.described', 'catalog.searched', 'catalog.fallback', 'task.question', 'task.resumed', 'agent.step'}
    if any(e['kind'] in blocked for e in events):
        return False
    requests = [e for e in events if e['kind'] == 'agent.model_request']
    batches = [e['data'] for e in events if e['kind'] == 'agent.single_batch']
    if len(requests) != 1 or len(batches) != 1 or batches[0]['digest'] != request_digest(arguments):
        return False
    if len(records) != 1:
        return False
    record = next(iter(records.values()))
    call = arguments['calls'][0]
    return (record['kind'] == 'control' and record['status'] == 'prepared'
            and record['tool_id'] == preloads[0]['tool_ids'][0] == call['tool_id']
            and record['call_id'] == call['call_id'] and record['arguments'] == call['arguments'])
