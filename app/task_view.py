"""Small business view; Hermes owns the transcript, the receipt ledger owns facts."""
import json


def task_view(store, task):
    with store.connect() as db:
        change = store.latest_change(task['id'], db)
        original = db.execute('SELECT original_goal FROM task_changes WHERE task_id=? ORDER BY revision LIMIT 1', (task['id'],)).fetchone()
        rows = db.execute('SELECT task_revision,response FROM capability_calls WHERE task_id=? ORDER BY created,rowid', (task['id'],)).fetchall()
    results = [json.loads(row['response']) for row in rows
               if not (change and row['task_revision'] < change['revision'] and json.loads(row['response']).get('kind') == 'control')]
    definitions = []
    for event in store.task_navigation(task['id']):
        for tid in event.get('tool_ids', []) + event.get('related_tool_ids', []) + event.get('schema_ids', []):
            if tid not in definitions:
                definitions.append(tid)
    parent_context = None
    if task.get('parent_task_id'):
        parent = store.task(task['parent_task_id'], task['session_id'])
        parent_context = {'task_id':parent['id'],'goal':parent['goal'],'status':parent['status'],
                          'summary':parent['summary'],'calls':parent['calls'], 'note':'关联任务历史，不代表本任务已执行'}
    return {'parent_context':parent_context, 'task_id':task['id'], 'task_revision':task['revision'],
            'original_goal':original[0] if original else task['goal'], 'effective_goal':task['goal'],
            'confirmed_constraints':change['constraints'] if change else {},
            'known_tool_ids':definitions, 'result_refs':[r['result_id'] for r in results],
            'question':task['question'], 'existing_results':results}
