"""Probe-only task/receipt guards for one explicit climate change; no parameter rewriting."""
import json
import time

from app.capability_tools import CapabilityTools
from app.store import Store

TOOL = 'control.climate.set_temperature'


def setup(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS steer_probe_cases(
          task_id TEXT PRIMARY KEY, name TEXT, protect INTEGER, barrier TEXT,
          reached INTEGER DEFAULT 0, released INTEGER DEFAULT 0,
          changed_at REAL, expected_revision INTEGER DEFAULT 0,
          old_goal TEXT, target_temperature INTEGER DEFAULT 23)''')


def case(store, tid):
    with store.connect() as db:
        row=db.execute('SELECT * FROM steer_probe_cases WHERE task_id=?',(tid,)).fetchone()
        return dict(row) if row else None


def register_case(store, task, name, protect, barrier):
    with store.connect() as db:
        db.execute('INSERT INTO steer_probe_cases(task_id,name,protect,barrier,old_goal) VALUES(?,?,?,?,?)',
                   (task['id'],name,int(protect),barrier,task['goal']))


def revise(store, tid, sid, goal, temperature=21):
    """A validated UI change in the probe; this is NOT a generic natural-language parser."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        task=store.decode(db.execute('SELECT * FROM tasks WHERE id=? AND session_id=?',(tid,sid)).fetchone())
        if task['status']!='running':raise ValueError('TASK_NOT_RUNNING')
        c=db.execute('SELECT * FROM steer_probe_cases WHERE task_id=?',(tid,)).fetchone()
        if c['changed_at'] is not None:raise ValueError('ALREADY_REVISED')
        revision=task['revision']+1 if c['protect'] else task['revision']
        now=time.time()
        db.execute('UPDATE steer_probe_cases SET changed_at=?,expected_revision=?,target_temperature=? WHERE task_id=?',(now,revision,temperature,tid))
        if c['protect']:
            # Old receipts stay in the ledger, but no longer count as active previews.
            db.execute("UPDATE tasks SET goal=?,revision=?,calls='[]',summary=NULL,outcome=NULL,final_result=NULL,updated=? WHERE id=?",(goal,revision,now,tid))
        store._event(db,task,'probe.goal_change',{'goal':goal,'task_revision':revision,'protected':bool(c['protect'])})
    return revision


def release(store,tid):
    with store.connect() as db:db.execute('UPDATE steer_probe_cases SET released=1 WHERE task_id=?',(tid,))


class ProbeTools(CapabilityTools):
    def _call(self,name,arguments):
        c=case(self.store,arguments.get('task_id','')) if isinstance(arguments,dict) else None
        # Keep original schema/context validation; only apply this additional guard
        # to a well-shaped call already carrying the current revision.
        validator=self.validators.get(name)
        if c and c['protect'] and c['changed_at'] and validator and validator.is_valid(arguments):
            if name=='call_tools' and arguments.get('task_revision')==c['expected_revision']:
                snap=self.catalog.snapshot(self.store.task(c['task_id'])['catalog_revision'])
                for call in arguments['calls']:
                    tool=snap.tools.get(call['tool_id'],{})
                    if tool.get('kind')=='control' and (call['tool_id']!=TOOL or call['arguments']!={'zone':'all','temperature_c':c['target_temperature']}):
                        return self.error('OUTDATED_TARGET','该调用不符合本次明确修改后的目标，请按最新目标生成参数。')
        return super()._call(name,arguments)

    def finish(self,initial,snap,outcome,summary,result_ids,unresolved=None,fast_arguments=None):
        c=case(self.store,initial['id'])
        if c and c['protect'] and c['changed_at']:
            with self.store.connect() as db:
                for rid in result_ids:
                    row=db.execute('SELECT task_revision,response FROM capability_calls WHERE id=? AND task_id=?',(rid,initial['id'])).fetchone()
                    if row and json.loads(row['response']).get('kind')=='control' and row['task_revision']!=c['expected_revision']:
                        return self.error('SUPERSEDED_EVIDENCE','旧轮次控制回执已被替代，不能完成修改后的目标。')
        return super().finish(initial,snap,outcome,summary,result_ids,unresolved,fast_arguments)
