"""Record shared user-facing messages without creating a second Agent execution."""
import hashlib


def input_key(prefix, *parts):
    return prefix+':'+hashlib.sha256('\0'.join(str(p) for p in parts).encode()).hexdigest()


def record_input(log, sid, key, text, device_id=None, source='text'):
    log.sync_events(sid)
    return log.append(sid,key,'user',text,source,device_id=device_id)


def record_result(log, sid, key, result, device_id=None, source='text', reply=True):
    task=result.get('task')
    task_id=task.get('id') if task else result.get('task_id')
    plan=result.get('plan')
    plan_id=plan.get('id') if plan else result.get('plan_id')
    if task_id or plan_id:
        log.update_by_key(sid,key,task_id=task_id,plan_id=plan_id)
    log.sync_events(sid)
    # Final task outcomes/questions already have a canonical source-event message.
    terminal=bool(task and task.get('status') in {'generated','completed','failed','cancelled','expired','unsupported','partial','interrupted','awaiting_input'})
    if reply and result.get('message') and not terminal:
        log.append(sid,key+':reply','assistant',result['message'],source,device_id=device_id,
                   task_id=task_id,plan_id=plan_id,kind='reply')
    return result
