"""Minimal probe coordinator; permits only an explicitly registered same-run revision."""
import asyncio,time
from app.hermes import TaskManager
from app.direct_completion import eligible
from app.store import TERMINAL
from .adapter import case


class ProbeManager(TaskManager):
    async def run(self,initial):
        tid=initial['id'];run_id=None
        try:
            pre=self.preloader.prepare(self.catalog.snapshot(initial['catalog_revision']),initial['goal'],initial['discovery_mode'],initial['revision'])
            self.store.event(tid,'catalog.preloaded' if pre['status']=='preloaded' else 'catalog.preload_skipped',{k:v for k,v in pre.items() if k not in {'tools','related_tools'}})
            context={**initial,'existing_results':self.store.capability_results(tid)}
            if pre['status']=='preloaded':context['candidate_tools']={'catalog_revision':pre['catalog_revision'],'tools':pre['tools']+pre.get('related_tools',[]),'primary_tool_ids':pre['tool_ids'],'related_tool_ids':pre.get('related_tool_ids',[]),'directory_hints':pre.get('directory_hints',[])}
            if eligible(initial,pre):context['completion_policy']={'mode':'validated_single_call'}
            run_id=await self.hermes.start(context,self.instructions())
            self.store.set_run(tid,run_id);self.store.event(tid,'task.dispatched',{'task_revision':initial['revision']})
            deadline=time.monotonic()+110
            while time.monotonic()<deadline:
                local=self.store.task(tid);c=case(self.store,tid)
                if local['revision']!=c['expected_revision']:
                    raise ValueError('Unregistered revision change')
                remote=await self.hermes.status(run_id)
                if local['status'] in TERMINAL:
                    # Let native end/our local completion middleware settle the same run.
                    if remote.get('status') in {'completed','failed','cancelled','interrupted'}:return
                elif remote.get('status') in {'completed','failed','cancelled','interrupted','unknown','incomplete'}:
                    self.store.finish(tid,'failed','Hermes ended without a valid current-goal completion');return
                await asyncio.sleep(.05)
            self.store.finish(tid,'failed','Probe timed out')
            await self.stop_remote(run_id)
        except asyncio.CancelledError:
            await self.stop_remote(run_id);raise
        except Exception as exc:
            self.store.event(tid,'probe.error',{'type':type(exc).__name__})
            self.store.finish(tid,'failed','Probe failed; inspect sanitized evidence')
            await self.stop_remote(run_id)


class ObservedStockManager(TaskManager):
    """The original run loop, with an audit record around its stop call only."""
    async def stop_remote(self,run_id):
        with self.store.connect() as db:
            row=db.execute('SELECT id FROM tasks WHERE run_id=?',(run_id,)).fetchone()
        if row:self.store.event(row['id'],'probe.stock_stop_requested',{})
        accepted=await super().stop_remote(run_id)
        if row:self.store.event(row['id'],'probe.stock_stop_returned',{'accepted':accepted})
        return accepted
