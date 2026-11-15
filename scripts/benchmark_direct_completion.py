"""Compare completion-only optimization with candidate preloading ON in both arms."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
import uuid

import httpx
from benchmark_preload import assess

ROOT=Path(__file__).resolve().parent.parent


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',default='')
    parser.add_argument('--output',type=Path,default=ROOT/'test-results/direct-completion-ab.json')
    opts=parser.parse_args()
    cases=json.loads((ROOT/'benchmarks/preload-cases.json').read_text())['cases']
    simple=[c for c in cases if c['id'] in ['S01','S04','S05']]
    regressions=[c for c in cases if c['group']!='simple']
    plan=[]
    for repeat in range(2):
        for i,c in enumerate(simple):
            for mode in (['agent','auto'] if (repeat+i)%2==0 else ['auto','agent']):
                plan.append((repeat+1,c,mode))
    plan.extend((1,c,'auto') for c in regressions)
    if opts.ids: plan=[entry for entry in plan if entry[1]['id'] in opts.ids.split(',')]
    if not plan: raise RuntimeError('No selected cases')
    rows=[]
    path=opts.output
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',timeout=20,trust_env=False) as client:
        (await client.post('/api/session')).raise_for_status()
        catalog=(await client.get('/api/catalog')).json()
        config=(await client.get('/api/config')).json()
        async def wait(tid):
            for _ in range(650):
                r=await client.get('/api/tasks/'+tid);r.raise_for_status();t=r.json()
                if t['status']!='running': return t
                await asyncio.sleep(.2)
            await client.post('/api/tasks/'+tid+'/cancel')
            raise RuntimeError('Task timeout')
        for repeat,case,mode in plan:
            r=await client.post('/api/tasks',json=dict(goal=case['goal'],profile_id=case['profile_id'],request_key=uuid.uuid4().hex,discovery_mode='auto',completion_mode=mode))
            r.raise_for_status();tid=r.json()['id'];task=await wait(tid);initial=task['status']
            if case.get('answer') and initial=='awaiting_input':
                (await client.post('/api/tasks/'+tid+'/answer',json={'answer':case['answer']})).raise_for_status();task=await wait(tid)
            await asyncio.sleep(.6)  # include any post-completion provider traffic in the count
            task=(await client.get('/api/tasks/'+tid)).json()
            passed,reasons,calls=assess(case,task,initial)
            awaiting_after_diagnosis = (case['group']=='failure' and task['status']=='awaiting_input' and bool(task.get('question')) and reasons==['must_report_blocked_control'])
            if awaiting_after_diagnosis: passed,reasons=True,[]
            timing=task['process']['timing']
            if task['catalog_revision']!=catalog['catalog_revision']: raise RuntimeError('Catalog changed')
            if case['group']=='simple' and mode=='auto' and (not timing['direct_completion'] or timing['provider_requests']!=1):
                reasons.append('expected_one_provider_request');passed=False
            if case['group']!='simple' and timing['direct_completion']:
                reasons.append('regression_must_keep_agent_completion');passed=False
            row=dict(id=case['id'],group=case['group'],repeat=repeat,mode=mode,goal=case['goal'],status=task['status'],passed=passed,reasons=reasons,calls=calls,timing=timing,question=task.get('question'),awaiting_after_diagnosis=awaiting_after_diagnosis)
            rows.append(row)
            path.write_text(json.dumps(dict(model=config['backend_model'],catalog_revision=catalog['catalog_revision'],method='same candidate preloading, alternating agent/auto completion for simple cases; auto regression suite',trials=rows),ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:row[k] for k in ['id','mode','repeat','status','passed','reasons','timing']},ensure_ascii=False),flush=True)
            if task['status']=='awaiting_input': await client.post('/api/tasks/'+tid+'/cancel')
        summary={mode:dict(n=len(v:=[r for r in rows if r['group']=='simple' and r['mode']==mode]),median_ms=round(statistics.median(r['timing']['total_ms'] for r in v),2) if v else None,provider_requests=[r['timing']['provider_requests'] for r in v]) for mode in ['agent','auto']}
        summary['passed']=sum(r['passed'] for r in rows);summary['total']=len(rows)
        summary['median_reduction_percent']=round((1-summary['auto']['median_ms']/summary['agent']['median_ms'])*100,1) if summary['agent']['n'] and summary['auto']['n'] else None
        report=json.loads(path.read_text());report['summary']=summary;path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(summary,ensure_ascii=False),flush=True)
        if summary['passed']!=summary['total']: raise SystemExit(1)


if __name__=='__main__': asyncio.run(main())
