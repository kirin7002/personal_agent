"""Paired real-Hermes A/B: same cases/model/catalog, sequential runs, alternating order."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import time
import uuid

import httpx

ROOT=Path(__file__).resolve().parent.parent


def assess(case, task, initial_status):
    calls=[{'tool':c['tool'],'arguments':c['arguments']} for c in task['calls']]
    expected=case['expected_calls']
    call_ok=len(calls)==len(expected) and all(c in calls for c in expected)
    reasons=[]
    if not call_ok: reasons.append('tool_or_arguments_mismatch')
    if case.get('expect_rejection_or_question'):
        explanation=(task.get('question') or '')+(task.get('summary') or '')
        rejected=any(r.get('error',{}).get('code')=='INVALID_ARGUMENTS' for r in task.get('results',[]) if r.get('error'))
        if not (initial_status=='awaiting_input' or (task.get('outcome') in {'unsupported','failed','partial'} and (rejected or ('45' in explanation and any(w in explanation for w in ['范围','上限','不支持']))))):
            reasons.append('invalid_value_not_rejected_or_clarified')
    elif case['group']=='failure':
        if task['status'] not in {'unsupported','partial'}: reasons.append('must_report_blocked_control')
    elif task['status']!='generated': reasons.append('task_not_completed')
    if case.get('answer') and initial_status!='awaiting_input': reasons.append('missing_required_clarification')
    queries=[r for r in task.get('results',[]) if r['status']=='returned_mock']
    if case.get('require_any_query') and not queries: reasons.append('missing_followup_query')
    domains={r['kind'] for r in queries}
    if not set(case.get('required_query_domains',[]))<=domains: reasons.append('missing_followup_query')
    evidence=case.get('expected_evidence')
    if evidence:
        matched=any(r.get('error',{}).get('code')==case.get('expected_failure_code') for r in task.get('results',[]) if r.get('error'))
        for result in queries:
            if matched: break
            if result['tool_id']!=evidence['tool']: continue
            value=result['data']
            try:
                for key in evidence['path']: value=value[key]
                matched=value==evidence['value'] and type(value) is type(evidence['value'])
            except (KeyError,TypeError): pass
            if matched: break
        if not matched: reasons.append('missing_expected_failure_evidence')
    return not reasons,reasons,calls


def summarize(rows, metadata):
    groups={}
    for group in sorted({r['group'] for r in rows}):
        groups[group]={}
        for mode in ['off','auto']:
            subset=[r for r in rows if r['group']==group and r['mode']==mode]
            if not subset: continue
            times=sorted(r['server_seconds'] for r in subset)
            groups[group][mode]={'n':len(subset),'passed':sum(r['passed'] for r in subset),'accuracy':round(sum(r['passed'] for r in subset)/len(subset),4),'median_seconds':round(statistics.median(times),3),'p90_seconds':round(times[max(0,math.ceil(.9*len(times))-1)],3),'median_directory_calls':statistics.median(r['directory_calls'] for r in subset),'preloaded':sum(r['preloaded'] for r in subset),'fallback':sum(r['fallback'] for r in subset)}
    simple=groups.get('simple',{})
    gain=None
    if 'off' in simple and 'auto' in simple:
        gain=round((1-simple['auto']['median_seconds']/simple['off']['median_seconds'])*100,1)
    return {**metadata,'groups':groups,'simple_median_reduction_percent':gain,'failures':[{'id':r['id'],'repeat':r['repeat'],'mode':r['mode'],'reasons':r['failure_reasons']} for r in rows if not r['passed']]}


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',default='')
    parser.add_argument('--repeats',type=int,default=2,choices=[1,2,3])
    parser.add_argument('--output',type=Path,default=ROOT/'test-results/preload-ab.json')
    args=parser.parse_args()
    cases=json.loads((ROOT/'benchmarks/preload-cases.json').read_text())['cases']
    if args.ids: cases=[c for c in cases if c['id'] in args.ids.split(',')]
    if not cases: raise RuntimeError('No selected cases')
    rows=[]
    args.output.parent.mkdir(exist_ok=True,parents=True)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',timeout=20,trust_env=False) as client:
        (await client.post('/api/session')).raise_for_status()
        config=(await client.get('/api/config')).json()
        catalog=(await client.get('/api/catalog')).json()
        if not config['hermes_configured']: raise RuntimeError('Hermes is not configured')
        metadata={'model':config['backend_model'],'catalog_revision':catalog['catalog_revision'],'repeats':args.repeats,'case_count':len(cases),'method':'sequential paired trials, alternating off/auto order; fresh task and Hermes session per trial','latency_scope':'server task creation to terminal result (or clarification); excludes S2S/audio and human think time','business_data':'mock','created_at_unix':time.time()}
        async def wait_task(tid):
            deadline=time.monotonic()+155
            while time.monotonic()<deadline:
                response=await client.get('/api/tasks/'+tid);response.raise_for_status();task=response.json()
                if task['status']!='running': return task
                await asyncio.sleep(.2)
            await client.post('/api/tasks/'+tid+'/cancel')
            raise RuntimeError('Benchmark task timed out')
        for repeat in range(args.repeats):
            for index,case in enumerate(cases):
                order=['off','auto'] if (repeat+index)%2==0 else ['auto','off']
                for mode in order:
                    start=time.monotonic()
                    response=await client.post('/api/tasks',json={'goal':case['goal'],'profile_id':case['profile_id'],'discovery_mode':mode,'completion_mode':'agent','request_key':uuid.uuid4().hex})
                    response.raise_for_status();tid=response.json()['id']
                    task=await wait_task(tid)
                    initial_status=task['status'];first_seconds=task['elapsed_seconds']
                    if case.get('answer') and initial_status=='awaiting_input':
                        (await client.post('/api/tasks/'+tid+'/answer',json={'answer':case['answer']})).raise_for_status()
                        task=await wait_task(tid)
                    if task['catalog_revision']!=metadata['catalog_revision']: raise RuntimeError('Catalog changed during comparison')
                    passed,reasons,calls=assess(case,task,initial_status)
                    nav=task['navigation']
                    preloads=[n for n in nav if n['kind']=='catalog.preloaded']
                    skips=[n['reason'] for n in nav if n['kind']=='catalog.preload_skipped']
                    row={'id':case['id'],'group':case['group'],'repeat':repeat+1,'mode':mode,'goal':case['goal'],'profile_id':case['profile_id'],'passed':passed,'failure_reasons':reasons,'initial_status':initial_status,'status':task['status'],'outcome':task.get('outcome'),'server_seconds':task['elapsed_seconds'],'first_response_seconds':first_seconds,'client_seconds':round(time.monotonic()-start,3),'directory_calls':sum(n['kind'] in {'catalog.browsed','catalog.described'} for n in nav),'preloaded':bool(preloads),'candidate_ids':[t for n in preloads for t in n['tool_ids']],'preload_ms':sum(n['elapsed_ms'] for n in preloads),'fallback':any(n['kind']=='catalog.fallback' for n in nav),'skip_reasons':skips,'calls':calls,'summary':task.get('summary'),'question':task.get('question')}
                    rows.append(row)
                    args.output.write_text(json.dumps({'summary':summarize(rows,metadata),'trials':rows},ensure_ascii=False,indent=2)+'\n')
                    print(json.dumps({k:row[k] for k in ['id','repeat','mode','passed','server_seconds','directory_calls','preloaded','fallback','failure_reasons']},ensure_ascii=False),flush=True)
                    if task['status']=='awaiting_input': await client.post('/api/tasks/'+tid+'/cancel')
                    # Allow a just-completed remote turn to stop before the next trial.
                    await asyncio.sleep(.6)
    print(json.dumps(summarize(rows,metadata),ensure_ascii=False),flush=True)
    if not all(row['passed'] for row in rows):
        raise SystemExit(1)


if __name__=='__main__': asyncio.run(main())
