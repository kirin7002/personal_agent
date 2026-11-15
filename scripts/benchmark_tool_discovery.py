"""Live before/after tool-discovery comparison. Always mock business data."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx
from benchmark_preload import assess

ROOT=Path(__file__).resolve().parent.parent
CASES=[
 {'id':'R01','group':'relative','goal':'空调温度增加一度','profile_id':'demo-ready','expected_calls':[{'tool':'control.climate.set_temperature','arguments':{'zone':'all','temperature_c':23}}],'require_any_query':True},
 {'id':'R02','group':'relative','goal':'主驾空调温度增加一度','profile_id':'demo-ready','expected_calls':[{'tool':'control.climate.set_temperature','arguments':{'zone':'driver','temperature_c':23}}],'require_any_query':True},
]
base=json.loads((ROOT/'benchmarks/preload-cases.json').read_text())['cases']
CASES += [c for c in base if c['id'] in ['S01','C02','F01']]
CASES += [{**CASES[0],'id':'R01-repeat'}]

async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--phase',required=True,choices=['before','after']);parser.add_argument('--ids',default='');opts=parser.parse_args()
 cases=[c for c in CASES if not opts.ids or c['id'] in opts.ids.split(',')]
 rows=[];out=ROOT/f'test-results/tool-discovery-{opts.phase}.json'
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,timeout=20) as client:
  (await client.post('/api/session')).raise_for_status()
  cfg=(await client.get('/api/config')).json();cat=(await client.get('/api/catalog')).json()
  async def wait(tid):
   for _ in range(700):
    r=await client.get('/api/tasks/'+tid);r.raise_for_status();t=r.json()
    if t['status']!='running':return t
    await asyncio.sleep(.2)
   await client.post('/api/tasks/'+tid+'/cancel');raise RuntimeError('Timed out')
  for case in cases:
   r=await client.post('/api/tasks',json={'goal':case['goal'],'profile_id':case['profile_id'],'request_key':uuid.uuid4().hex,'discovery_mode':'auto','completion_mode':'auto'})
   r.raise_for_status();tid=r.json()['id'];task=await wait(tid);initial=task['status'];clarification=None
   if initial=='awaiting_input' and case['group']=='relative':
    clarification=task['question'];answer='主驾增加一度' if case['id']=='R02' else '全部分区各增加一度'
    (await client.post('/api/tasks/'+tid+'/answer',json={'answer':answer})).raise_for_status();task=await wait(tid)
   await asyncio.sleep(.5);task=(await client.get('/api/tasks/'+tid)).json()
   ok,reasons,calls=assess(case,task,initial)
   if case['group']=='failure' and task['status']=='awaiting_input' and reasons==['must_report_blocked_control']:ok,reasons=True,[]
   steps=task['process']['steps'];nav=task['navigation'];timing=task['process']['timing']
   row={'id':case['id'],'goal':case['goal'],'passed':ok,'reasons':reasons,'status':task['status'],'clarification':clarification,'calls':calls,'seconds':task['elapsed_seconds'],'timing':timing,'navigation':nav,'operations':[{'title':s['title'],'duration_ms':s['duration_ms'],'observation':s.get('observation')} for s in steps if s['duration_kind']=='measured_tool']}
   rows.append(row)
   out.write_text(json.dumps({'phase':opts.phase,'model':cfg['backend_model'],'catalog_revision':cat['catalog_revision'],'trials':rows},ensure_ascii=False,indent=2))
   print(json.dumps({k:row[k] for k in ['id','passed','status','seconds','reasons']}|{'directory_calls':sum(n['kind'] in ['catalog.browsed','catalog.described'] for n in nav),'search_calls':sum(n['kind']=='catalog.searched' for n in nav),'provider_requests':timing.get('provider_requests')},ensure_ascii=False),flush=True)
   if task['status']=='awaiting_input':await client.post('/api/tasks/'+tid+'/cancel')
   await asyncio.sleep(.5)
 if not all(r['passed'] for r in rows):raise SystemExit(1)

if __name__=='__main__':asyncio.run(main())
